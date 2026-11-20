# -*- coding: utf-8 -*-
"""
@author: Patrick Cleeve, Alexéy Ilyushkin

Copyright © 2025-2026 Patrick Cleeve, Alexéy Ilyushkin, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License version 2 as published by the Free
Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.


### Purpose ###

This module contains classes to control the actions related to the milling.

"""
import logging
import os
import threading
from concurrent.futures import Future
from concurrent.futures._base import CANCELLED, FINISHED, RUNNING, CancelledError
from datetime import datetime
from enum import Enum
from typing import Dict, List


from odemis import model
from odemis.acq.acqmng import acquire
from odemis.acq.feature import (
    FEATURE_DEACTIVE,
    FEATURE_POLISHED,
    FEATURE_ROUGH_MILLED,
    FEATURE_ACTIVE,
    FEATURE_READY_TO_MILL,
    CryoFeature,
)
from odemis.acq.milling.tasks import MillingTaskSettings
from odemis.acq.milling.patterns import (
    CorrelationPatternParameters,
    NotchPatternParameters,
    RulerPatternParameters,
    WaffleTrenchPatternParameters,
)
from odemis.acq.milling.fibsemos import run_milling_tasks_fibsemos
from odemis.acq.move import Posture, MicroscopePostureManager
from odemis.acq.stream import FIBStream, SEMStream
from odemis.dataio import find_fittest_converter
from odemis.util import executeAsyncTask


class MillingWorkflowTask(Enum):
    RoughMilling = "Rough Milling"
    Polishing = "Polishing"

status_map: Dict[MillingWorkflowTask, str] = {
    MillingWorkflowTask.RoughMilling: FEATURE_ROUGH_MILLED,
    MillingWorkflowTask.Polishing: FEATURE_POLISHED,
}
def get_associated_tasks(wt: MillingWorkflowTask,
                         milling_tasks: Dict[str, MillingTaskSettings]) -> List[MillingTaskSettings]:
    """Get the milling tasks associated with the given workflow task.
    :param wt: The workflow task to get associated tasks for.
    :param milling_tasks: The dictionary of all milling tasks.
    :return: List of associated tasks."""
    associated_tasks = []
    for task in milling_tasks.values():
        if not task.selected:
            continue

        # Optional rectangle patterns always belong to rough milling, regardless of task name.
        is_rough_pattern = any(
            isinstance(pattern, (
                CorrelationPatternParameters,
                RulerPatternParameters,
                NotchPatternParameters,
                WaffleTrenchPatternParameters,
            ))
            for pattern in task.patterns)
        if is_rough_pattern:
            if wt is MillingWorkflowTask.RoughMilling:
                associated_tasks.append(task)
            continue

        # TODO: Replace name matching and special cases with explicit workflow assignments.
        if wt is MillingWorkflowTask.RoughMilling and "Microexpansion" in task.name:
            associated_tasks.insert(0, task)  # This must be the first task.
        elif wt.value in task.name:
            associated_tasks.append(task)

    return associated_tasks

class AutomatedMillingManager(object):

    def __init__(self,
                 future: Future,
                 features: List[CryoFeature],
                 stage: model.Actuator,
                 sem_stream: SEMStream,
                 fib_stream: FIBStream,
                 task_list: List[MillingWorkflowTask],
                 ):

        self.stage = stage
        self.sem_stream = sem_stream
        self.fib_stream = fib_stream
        self.ion_beam = fib_stream.emitter
        self.features = features
        self.task_list = task_list
        self._exporter = find_fittest_converter("filename.ome.tiff")
        self.pm = MicroscopePostureManager(model.getMicroscope())
        self._prefix: str = ""

        self._future = future
        if future is not None:
            self._future.running_subf = model.InstantaneousFuture()
            self._future._task_lock = threading.Lock()

    def cancel(self, future: Future) -> bool:
        """
        Canceler of milling task.
        :param future: the future that will be executing the task
        :return: True if it successfully cancelled (stopped) the future
        """
        logging.debug("Canceling milling procedure...")

        with future._task_lock:
            if future._task_state == FINISHED:
                return False
            future._task_state = CANCELLED
            future.running_subf.cancel()
            logging.debug("Milling procedure cancelled.")
        return True

    def run(self):
        self._future._task_state = RUNNING

        for task_num, workflow_task in enumerate(self.task_list, 1):

            self.current_workflow = workflow_task.value
            logging.info(f"Starting {task_num}/{len(self.task_list)}: {self.current_workflow} for {len(self.features)} features...")

            current_posture = self.pm.get_current_posture()
            if current_posture not in [Posture.SEM_IMAGING, Posture.MILLING, Posture.TRENCHING]:
                error_text = (f"Current posture is {current_posture}. "
                               "Please switch to SEM_IMAGING, MILLING, or TRENCHING before starting automated milling.")
                logging.error(error_text)
                raise ValueError(error_text)

            for feature in self.features:

                if feature.status.value == FEATURE_DEACTIVE:
                    logging.info(f"Skipping {feature.name.value} as it is deactivated.")
                    continue
                elif feature.status.value == FEATURE_ACTIVE:
                    logging.info(f"Skipping {feature.name.value} as it is not ready for milling.")
                    continue
                elif status_map[workflow_task] == feature.status.value == FEATURE_ROUGH_MILLED:
                    logging.info(f"Skipping {feature.name.value} as it was already rough milled.")
                    continue
                elif status_map[workflow_task] == feature.status.value == FEATURE_POLISHED:
                    logging.info(f"Skipping {feature.name.value} as it was already polished.")
                    continue
                elif workflow_task == MillingWorkflowTask.RoughMilling and feature.status.value == FEATURE_POLISHED:
                    logging.info(f"Skipping {feature.name.value} as it was already rough milled and polished.")
                    continue

                # get milling tasks
                milling_tasks = get_associated_tasks(
                    wt=workflow_task,
                    milling_tasks=feature.milling_tasks)

                if not milling_tasks:
                    logging.info(f"Skipping {feature.name.value} as it has no tasks to mill.")
                    continue

                # prefix for images
                self._prefix = f"{feature.name.value}-{self.current_workflow}"

                self._future.msg = f"{feature.name.value}: Starting {self.current_workflow}"
                self._future.current_feature = feature
                self._future.set_progress()

                logging.info(f"Starting {self.current_workflow} for {feature.name.value}, status: {feature.status.value}")

                ############# STAGE MOVEMENT #############
                self._move_to_milling_position(feature)

                ############# MILLING #############
                self._run_milling_tasks(feature, milling_tasks)

                ############# REFERENCE IMAGING #############
                self._acquire_reference_images(feature)

                # update status
                feature.status.value = status_map[workflow_task]

                logging.info(f"Finished {self.current_workflow} for {feature.name.value}")

        # TODO: implement fm imaging between workflow tasks
        # TODO: configuraable settings for sem/fib imaging
        # TODO: configurable workflow for flm imaging

    def check_cancelled(self):
        with self._future._task_lock:
            if self._future.cancelled() == CANCELLED:
                raise CancelledError()

    def _move_to_milling_position(self, feature: CryoFeature) -> None:

        self.check_cancelled()
        self._future.msg = f"{feature.name.value}: Moving to Milling Position"
        self._future.set_progress()

        # milling position
        stage_position = feature.get_posture_position(Posture.MILLING)

        # move to position
        self._future.running_subf = self.stage.moveAbs(stage_position)
        self._future.running_subf.result()

        self._future.msg = f"Moved to {feature.name.value}"
        self._future.set_progress()

    def _run_milling_tasks(self, feature: CryoFeature, milling_tasks: List[MillingTaskSettings]) -> None:
        """Run the milling tasks for the given feature."""

        self._future.msg = f"{feature.name.value}: Milling: {self.current_workflow}"
        self._future.set_progress()

        self._future.running_subf = run_milling_tasks_fibsemos(tasks=milling_tasks, feature=feature, path=feature.path)
        self._future.running_subf.result()

    def _acquire_reference_images(self, feature: CryoFeature) -> None:
        self.check_cancelled()

        self._future.msg = f"{feature.name.value}: Acquiring Reference Images"
        self._future.set_progress()

        # acquire images
        self._future.running_subf = acquire([self.sem_stream, self.fib_stream])
        data, ex = self._future.running_subf.result()
        sem_image, fib_image = data

        # save images
        sem_filename = self.get_filename(feature, "Finished-SEM")
        fib_filename = self.get_filename(feature, "Finished-FIB")
        self._exporter.export(sem_filename, sem_image)
        self._exporter.export(fib_filename, fib_image)

    def get_filename(self, feature: CryoFeature, basename: str) -> str:
        """Get a unique filename for the given feature and basename.
        :param feature: The feature to get the filename for.
        :param basename: The basename of the filename.
        :return: The full filename."""
        ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        filename = f"{self._prefix}-{basename}-{ts}.ome.tiff".replace(" ", "-")
        return os.path.join(os.path.join(feature.path, filename))


def run_automated_milling(features: List[CryoFeature],
                          stage: model.Actuator,
                          sem_stream: SEMStream,
                          fib_stream: FIBStream,
                          task_list: List[MillingWorkflowTask],
                          ) -> Future:
    """
    Automatically mill and image a list of features.

    :return: ProgressiveFuture
    """
    # Create a progressive future with running sub future
    future = model.ProgressiveFuture()
    # create automated milling task
    amm = AutomatedMillingManager(
        future=future,
        stage=stage,
        sem_stream=sem_stream,
        fib_stream=fib_stream,
        task_list=task_list,
        features=features,
    )
    # add the ability of cancelling the future during execution
    future.task_canceller = amm.cancel

    # set the progress of the future
    total_duration = len(task_list) * len(features) * 30
    future.set_progress(remaining_time=total_duration) # TODO: get proper time estimate from fibsemOS

    # assign the acquisition task to the future
    executeAsyncTask(future, amm.run)

    return future
