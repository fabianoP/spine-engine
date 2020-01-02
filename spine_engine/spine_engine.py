######################################################################################################################
# Copyright (C) 2017 - 2019 Spine project consortium
# This file is part of Spine Engine.
# Spine Engine is free software: you can redistribute it and/or modify it under the terms of the GNU Lesser General
# Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option)
# any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General
# Public License for more details. You should have received a copy of the GNU Lesser General Public License along with
# this program. If not, see <http://www.gnu.org/licenses/>.
######################################################################################################################

"""
Contains the SpineEngine class for running Spine Toolbox DAGs.

:authors: M. Marin (KTH)
:date:   20.11.2019
"""

from itertools import chain
from enum import Enum
from dagster import (
    PipelineDefinition,
    SolidDefinition,
    InputDefinition,
    OutputDefinition,
    DependencyDefinition,
    Output,
    Failure,
    execute_pipeline_iterator,
    DagsterEventType,
)


def _inverted(input_):
    """Inverts a dictionary of list values.

    Args:
        input_ (dict)

    Returns:
        dict: keys are list items, and values are keys listing that item from the input dictionary
    """
    output = dict()
    for key, value_list in input_.items():
        for value in value_list:
            output.setdefault(value, list()).append(key)
    return output


class SpineEngineState(Enum):
    SLEEPING = 1
    RUNNING = 2
    USER_STOPPED = 3
    FAILED = 4
    COMPLETED = 5


class SpineEngine:
    """
    An engine for executing a Spine Toolbox DAG-workflow.

    The engine consists of two pipelines:
    - One backwards, where ProjectItems collect resources from successor items if applies
    - One forward, where actual execution happens.
    """

    def __init__(self, project_items, successors, execution_permits):
        """
        Creates the two pipelines.

        Args:
            project_items (list(ProjectItem)): The items to execute.
            successors (dict): A mapping from item name to list of successor item names, dictating the dependencies.
            execution_permits (dict): A mapping from item name to a boolean value, False indicating that
                the item is not executed, only its resources are collected.
        """
        # NOTE: The `name` argument in all dagster constructor is not allowed to have spaces,
        # so we need to use the `short_name` of our `ProjectItem`s.
        d = {item.name: item.short_name for item in project_items}
        back_injectors = {d[key]: [d[x] for x in value] for key, value in successors.items()}
        forth_injectors = _inverted(back_injectors)
        self._backward_pipeline = self._make_pipeline(project_items, back_injectors, "backward", execution_permits)
        self._forward_pipeline = self._make_pipeline(project_items, forth_injectors, "forward", execution_permits)
        self._project_item_lookup = {item.short_name: item for item in project_items}
        self._state = SpineEngineState.SLEEPING
        self._running_item = None

    def state(self):
        return self._state

    @classmethod
    def from_cwl(cls, path):
        """Returns an instance of this class from a CWL file.

        Args:
            path (str): Path to a CWL file with the DAG-workflow description.

        Returns:
            SpineEngine
        """
        # TODO

    def run(self):
        """Runs this engine.
        """
        self._state = SpineEngineState.RUNNING
        environment_dict = {"loggers": {"console": {"config": {"log_level": "CRITICAL"}}}}
        for event in chain(
            execute_pipeline_iterator(self._backward_pipeline, environment_dict=environment_dict),
            execute_pipeline_iterator(self._forward_pipeline, environment_dict=environment_dict),
        ):
            if event.event_type == DagsterEventType.STEP_START:
                self._running_item = self._project_item_lookup[event.solid_name]
            elif event.event_type == DagsterEventType.STEP_FAILURE:
                self._running_item = self._project_item_lookup[event.solid_name]
                if self._state != SpineEngineState.USER_STOPPED:
                    self._state = SpineEngineState.FAILED
        if self._state == SpineEngineState.RUNNING:
            self._state = SpineEngineState.COMPLETED

    def stop(self):
        """Stops this engine.
        """
        self._state = SpineEngineState.USER_STOPPED
        if self._running_item:
            self._running_item.stop_execution()

    def _make_pipeline(self, project_items, injectors, direction, execution_permits):
        """
        Returns a PipelineDefinition for executing the given items in the given direction,
        generating dependencies from the given injectors.

        Args:
            project_items (list(ProjectItem)): List of project items for creating pipeline solids.
            injectors (dict(str,list(str))): A mapping from item name to list of injector item names.
            direction (str): The direction of the pipeline, either "forward" or "backward".
            execution_permits (dict): A mapping from item name to a boolean value, False indicating that
                the item is not executed, only its resources are collected.

        Returns:
            PipelineDefinition
        """
        solid_defs = [
            self._make_solid_def(item, injectors, direction, execution_permits[item.name]) for item in project_items
        ]
        dependencies = self._make_dependencies(injectors)
        return PipelineDefinition(name=f"{direction}_pipeline", solid_defs=solid_defs, dependencies=dependencies)

    def _make_solid_def(self, item, injectors, direction, execute):
        """Returns a SolidDefinition for executing the given item in the given direction.

        Args:
            item (ProjectItem): The project item that gets executed by the solid.
            injectors (dict): Mapping from item name to list of injector item names.
            direction (str): The direction of execution, either "forward" or "backward".
            execute (bool): If False, do not execute the item, just collect resources.

        Returns:
            SolidDefinition
        """

        def compute_fn(context, inputs):
            if self.state() in (SpineEngineState.USER_STOPPED, SpineEngineState.FAILED):
                raise Failure()
            inputs = [val for values in inputs.values() for val in values]
            if execute and not item.execute(inputs, direction):
                raise Failure()
            yield Output(value=item.output_resources(direction), output_name="result")

        input_defs = [InputDefinition(name=f"input_from_{n}") for n in injectors.get(item.short_name, [])]
        output_defs = [OutputDefinition(name="result")]
        return SolidDefinition(
            name=item.short_name, input_defs=input_defs, compute_fn=compute_fn, output_defs=output_defs
        )

    @staticmethod
    def _make_dependencies(injectors):
        """
        Returns a dictionary of dependencies according to the given dictionary of injectors.

        Args:
            injectors (dict): Mapping from item name to list of injector item names.

        Returns:
            dict: a dictionary to pass to the PipelineDefinition constructor as dependencies
        """
        return {
            item_name: {f"input_from_{n}": DependencyDefinition(n, "result") for n in injector_names}
            for item_name, injector_names in injectors.items()
        }
