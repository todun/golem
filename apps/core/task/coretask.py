import abc
import copy
import logging
import os
import uuid

from enum import Enum
from typing import Type

from ethereum.utils import denoms

from apps.core.task.coretaskstate import CoreTaskDefaults, TaskDefinition, Options
from apps.core.task.verificator import CoreVerificator, SubtaskVerificationState
from golem.core.common import HandleKeyError, timeout_to_deadline, to_unicode, \
    timeout_to_string, string_to_timeout
from golem.core.compress import decompress
from golem.core.fileshelper import outer_dir_path
from golem.core.simpleserializer import CBORSerializer
from golem.docker.environment import DockerEnvironment
from golem.environments.environment import Environment
from golem.network.p2p.node import Node
from golem.resource.dirmanager import DirManager
from golem.resource.resource import prepare_delta_zip, TaskResourceHeader
from golem.task.taskbase import Task, TaskHeader, TaskBuilder, result_types, \
    resource_types, ComputeTaskDef
from golem.task.taskclient import TaskClient
from golem.task.taskstate import SubtaskStatus

from functools import wraps

logger = logging.getLogger("apps.core")


def log_key_error(*args, **kwargs):
    logger.warning("This is not my subtask {}".format(args[1]), exc_info=True)
    return False


class AcceptClientVerdict(Enum):
    ACCEPTED = 0
    REJECTED = 1
    SHOULD_WAIT = 2


MAX_PENDING_CLIENT_RESULTS = 1


class TaskTypeInfo(object):
    """ Information about task that allows to define and build a new task,
    display outputs and previews. """

    def __init__(self,
                 name: str,
                 definition: Type[TaskDefinition],
                 defaults: CoreTaskDefaults,
                 options: Type[Options],
                 task_builder_type: Type[TaskBuilder],
                 dialog=None,
                 dialog_controller=None):
        self.name = name
        self.defaults = defaults
        self.options = options
        self.definition = definition
        self.task_builder_type = task_builder_type
        self.dialog = dialog
        self.dialog_controller = dialog_controller
        self.output_formats = []
        self.output_file_ext = []

    @classmethod
    def get_task_num_from_pixels(cls, x, y, definition, total_subtasks,
                                 output_num=1):
        return 0

    @classmethod
    def get_task_border(cls, subtask, definition, total_subtasks,
                        output_num=1, as_path=False):
        return []

    @classmethod
    def get_preview(cls, task, single=False):
        pass

    @staticmethod
    def _preview_result(result, single=False):
        if single:
            return result
        if result is not None:
            if isinstance(result, dict):
                return result
            else:
                return {'1': result}
        return {}


class CoreTask(Task):
    VERIFICATOR_CLASS: Type[CoreVerificator] = CoreVerificator
    ENVIRONMENT_CLASS: Type[Environment] = None # TODO maybe @abstraact @property?

    handle_key_error = HandleKeyError(log_key_error)

    ################
    # Task methods #
    ################

    def __init__(self,
                 task_definition: TaskDefinition,
                 node_name: str,
                 owner_address="",
                 owner_port=0,
                 owner_key_id="",
                 max_pending_client_results=MAX_PENDING_CLIENT_RESULTS,
                 resource_size=None, # backward compatibility
                 root_path=None
                 ):
        """Create more specific task implementation
        """

        self.task_definition = task_definition
        task_timeout = task_definition.full_task_timeout
        deadline = timeout_to_deadline(task_timeout)

        # resources stuff
        self.task_resources = list(set(filter(os.path.isfile, task_definition.resources))) # why is there a set?
        if resource_size is None:
            self.resource_size = 0
            for resource in self.task_resources:
                self.resource_size += os.stat(resource).st_size
        else:
            self.resource_size = resource_size

        self.environment = self.ENVIRONMENT_CLASS()

        # src_code stuff
        self.main_program_file = self.environment.main_program_file
        try:
            with open(self.main_program_file, "r") as src_file:
                src_code = src_file.read()
        except Exception as err:
            logger.warning("Wrong main program file: {}".format(err))
            src_code = ""

        # docker_images stuff
        docker_images = None
        if task_definition.docker_images:
            docker_images = task_definition.docker_images
        elif isinstance(self.environment, DockerEnvironment):
            docker_images = self.environment.docker_images

        th = TaskHeader(
            node_name=node_name,
            task_id=task_definition.task_id,
            task_owner_address=owner_address,
            task_owner_port=owner_port,
            task_owner_key_id=owner_key_id,
            environment=self.environment.get_id(),
            task_owner=Node(),
            deadline=deadline,
            subtask_timeout=task_definition.subtask_timeout,
            resource_size=self.resource_size,
            estimated_memory=task_definition.estimated_memory,
            max_price=task_definition.max_price,
            docker_images=docker_images,
        )

        Task.__init__(self, th, src_code)

        self.total_tasks = 0
        self.last_task = 0

        self.num_tasks_received = 0
        self.subtasks_given = {}
        self.num_failed_subtasks = 0

        self.full_task_timeout = task_timeout
        self.counting_nodes = {}

        self.root_path = root_path

        self.stdout = {}  # for each subtask keep info about stdout received from computing node
        self.stderr = {}  # for each subtask keep info about stderr received from computing node
        self.results = {}  # for each subtask keep info about files containing results

        self.res_files = {}
        self.tmp_dir = None
        self.verificator = self.VERIFICATOR_CLASS()
        self.max_pending_client_results = max_pending_client_results


    def is_docker_task(self):
        return hasattr(self.header, 'docker_images') \
               and self.header.docker_images \
               and len(self.header.docker_images) > 0

    def initialize(self, dir_manager):
        self.tmp_dir = dir_manager.get_task_temporary_dir(self.header.task_id, create=True)
        self.verificator.tmp_dir = self.tmp_dir

    def needs_computation(self):
        return (self.last_task != self.total_tasks) or (self.num_failed_subtasks > 0)

    def finished_computation(self):
        return self.num_tasks_received == self.total_tasks

    def computation_failed(self, subtask_id):
        self._mark_subtask_failed(subtask_id)

    def computation_finished(self, subtask_id, task_result, result_type=0):
        if not self.should_accept(subtask_id):
            logger.info("Not accepting results for {}".format(subtask_id))
            return
        self.interpret_task_results(subtask_id, task_result, result_type)
        result_files = self.results.get(subtask_id)
        ver_state = self.verificator.verify(subtask_id, self.subtasks_given.get(subtask_id),
                                            result_files, self)
        if ver_state == SubtaskVerificationState.VERIFIED:
            self.accept_results(subtask_id, result_files)
        # TODO Add support for different verification states
        else:
            self.computation_failed(subtask_id)

    def accept_results(self, subtask_id, result_files):
        self.subtasks_given[subtask_id]['status'] = SubtaskStatus.finished

    @handle_key_error
    def verify_subtask(self, subtask_id):
        return self.subtasks_given[subtask_id]['status'] == SubtaskStatus.finished

    def verify_task(self):
        return self.finished_computation()

    def get_total_tasks(self):
        return self.total_tasks

    def get_active_tasks(self):
        return self.last_task

    def get_tasks_left(self):
        return (self.total_tasks - self.last_task) + self.num_failed_subtasks

    def get_subtasks(self, part):
        return []

    def restart(self):
        for subtask_id in list(self.subtasks_given.keys()):
            self.restart_subtask(subtask_id)

    @handle_key_error
    def restart_subtask(self, subtask_id):
        subtask_info = self.subtasks_given[subtask_id]
        was_failure_before = subtask_info['status'] in [SubtaskStatus.failure,
                                                        SubtaskStatus.resent]

        if SubtaskStatus.is_computed(subtask_info['status']):
            self._mark_subtask_failed(subtask_id)
        elif subtask_info['status'] == SubtaskStatus.finished:
            self._mark_subtask_failed(subtask_id)
            tasks = subtask_info['end_task'] - subtask_info['start_task'] + 1
            self.num_tasks_received -= tasks

        if not was_failure_before:
            subtask_info['status'] = SubtaskStatus.restarted

    def abort(self):
        pass

    def get_progress(self):
        if self.total_tasks == 0:
            return 0.0
        return self.num_tasks_received / self.total_tasks

    def get_resources(self, resource_header, resource_type=0, tmp_dir=None):

        dir_name = self._get_resources_root_dir()
        if tmp_dir is None:
            tmp_dir = self.tmp_dir

        if os.path.exists(dir_name):
            if resource_type == resource_types["zip"]:
                return prepare_delta_zip(dir_name, resource_header, tmp_dir, self.task_resources)

            elif resource_type == resource_types["parts"]:
                return TaskResourceHeader.build_parts_header_delta_from_chosen(resource_header,
                                                                               dir_name,
                                                                               self.res_files)
            elif resource_type == resource_types["hashes"]:
                return copy.copy(self.task_resources)

        return None

    def update_task_state(self, task_state):
        pass

    @handle_key_error
    def get_trust_mod(self, subtask_id):
        return 1.0

    def add_resources(self, res_files):
        self.res_files = res_files

    def get_stderr(self, subtask_id):
        return self.stderr.get(subtask_id, "")

    def get_stdout(self, subtask_id):
        return self.stdout.get(subtask_id, "")

    def get_results(self, subtask_id):
        return self.results.get(subtask_id, [])

    def to_dictionary(self):
        return {
            'id': to_unicode(self.header.task_id),
            'name': to_unicode(self.task_definition.task_name),
            'type': to_unicode(self.task_definition.task_type),
            'subtasks': self.get_total_tasks(),
            'progress': self.get_progress()
        }

    def _new_compute_task_def(self, hash, extra_data, working_directory=".", perf_index=0):
        ctd = ComputeTaskDef()
        ctd.task_id = self.header.task_id
        ctd.subtask_id = hash
        ctd.extra_data = extra_data
        ctd.short_description = self.short_extra_data_repr(extra_data)
        ctd.src_code = self.src_code
        ctd.performance = perf_index
        ctd.working_directory = working_directory
        ctd.docker_images = self.header.docker_images
        ctd.deadline = timeout_to_deadline(self.header.subtask_timeout)
        ctd.task_owner = self.header.task_owner
        ctd.environment = self.header.environment

        return ctd

    #########################
    # Specific task methods #
    #########################

    def interpret_task_results(self, subtask_id, task_results, result_type, sort=True):
        """Filter out ".log" files from received results. Log files should represent
        stdout and stderr from computing machine. Other files should represent subtask results.
        :param subtask_id: id of a subtask for which results are received
        :param task_results: it may be a list of files, if result_type is equal to
        result_types["files"] or it may be a cbor serialized zip file containing all files,
        if result_type is equal to result_types["data"]
        :param result_type: a number from result_types, it may represents data format or files
        format
        :param bool sort: *default: True* Sort results, if set to True
        """
        self.stdout[subtask_id] = ""
        self.stderr[subtask_id] = ""
        tr_files = self.load_task_results(task_results, result_type, subtask_id)
        self.results[subtask_id] = self.filter_task_results(tr_files, subtask_id)
        if sort:
            self.results[subtask_id].sort()

    @handle_key_error
    def result_incoming(self, subtask_id):
        self.counting_nodes[self.subtasks_given[subtask_id]['node_id']].finish()
        self.subtasks_given[subtask_id]['status'] = SubtaskStatus.downloading

    # TODO why is it here at all?
    def query_extra_data_for_test_task(self):
        return None  # Implement in derived methods

    def load_task_results(self, task_result, result_type, subtask_id):
        """ Change results to a list of files. If result_type is equal to result_types["files"} this
        function only return task_results without making any changes. If result_type is equal to
        result_types["data"] tham task_result is cbor and unzipped and files are saved in tmp_dir.
        :param task_result: list of files of cbor serialized ziped file with files
        :param result_type: result_types element
        :param str subtask_id:
        :return:
        """
        if result_type == result_types['data']:
            output_dir = os.path.join(self.tmp_dir, subtask_id)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            return [self._unpack_task_result(trp, output_dir) for trp in task_result]
        elif result_type == result_types['files']:
            return task_result
        else:
            logger.error("Task result type not supported {}".format(result_type))
            self.stderr[subtask_id] = "[GOLEM] Task result {} not supported".format(result_type)
            return []

    def filter_task_results(self, task_results, subtask_id, log_ext=".log", err_log_ext="err.log"):
        """ From a list of files received in task_results, return only files that don't
        have extension <log_ext> or <err_log_ext>. File with log_ext is saved as stdout
        for this subtask (only one file is currently supported). File with err_log_ext is save
        as stderr for this subtask (only one file is currently supported).
        :param list task_results: list of files
        :param str subtask_id: if of a given subtask
        :param str log_ext: extension that stdout files have
        :param str err_log_ext: extension that stderr files have
        :return:
        """

        filtered_task_results = []
        for tr in task_results:
            if tr.endswith(err_log_ext):
                self.stderr[subtask_id] = tr
            elif tr.endswith(log_ext):
                self.stdout[subtask_id] = tr
            else:
                try:
                    new_tr = outer_dir_path(tr)
                    if os.path.isfile(new_tr):
                        os.remove(new_tr)
                    os.rename(tr, new_tr)
                    filtered_task_results.append(new_tr)
                except (IOError, OSError) as err:
                    logger.warning("Cannot move file {} to new location: "
                                   "{}".format(tr, err))

        return filtered_task_results

    def after_test(self, results, tmp_dir):
        return {}

    def notify_update_task(self):
        for l in self.listeners:
            l.notify_update_task(self.header.task_id)

    @handle_key_error
    def should_accept(self, subtask_id):
        status = self.subtasks_given[subtask_id]['status']
        return SubtaskStatus.is_computed(status)

    @staticmethod
    def _interpret_log(log):
        if log is None:
            return ""
        if not os.path.isfile(log):
            return log
        try:
            with open(log) as f:
                res = f.read()
            return res
        except IOError as err:
            logger.error("Can't read file {}: {}".format(log, err))
            return ""

    @handle_key_error
    def _mark_subtask_failed(self, subtask_id):
        self.subtasks_given[subtask_id]['status'] = SubtaskStatus.failure
        self.counting_nodes[self.subtasks_given[subtask_id]['node_id']].reject()
        self.num_failed_subtasks += 1

    def _unpack_task_result(self, trp, output_dir):
        tr = CBORSerializer.loads(trp)
        with open(os.path.join(output_dir, tr[0]), "wb") as fh:
            fh.write(decompress(tr[1]))
        return os.path.join(output_dir, tr[0])

    def _get_resources_root_dir(self):
        task_resources = list(self.task_resources)
        prefix = os.path.commonprefix(task_resources)
        return os.path.dirname(prefix)

    def _accept_client(self, node_id):
        client = TaskClient.assert_exists(node_id, self.counting_nodes)
        finishing = client.finishing()
        max_finishing = self.max_pending_client_results

        if client.rejected():
            return AcceptClientVerdict.REJECTED
        elif finishing >= max_finishing or client.started() - finishing >= max_finishing:
            return AcceptClientVerdict.SHOULD_WAIT

        client.start()
        return AcceptClientVerdict.ACCEPTED

    # def _with_check(f):
    #     @wraps(f)
    #     def wrapped(inst, *args, **kwargs):
    #         if inst.check():
    #             return
    #         return f(inst, *args, **kwargs)
    #     return wrapped

    # def _accepting(query_extra_data_func):
    #     """
    #     A decorator for query_extra_data - it wraps the function with verification code
    #     :param query_extra_data:
    #     :return:
    #     """
    #     @wraps(query_extra_data_func)
    #     def accepting_qed(self, perf_index: float, num_cores=1, node_id: str = None,
    #                          node_name: str = None) -> Task.ExtraData:
    #         verdict = self._accept_client(node_id)
    #         if verdict != AcceptClientVerdict.ACCEPTED:
    #
    #             should_wait = verdict == AcceptClientVerdict.SHOULD_WAIT
    #             if should_wait:
    #                 logger.warning("Waiting for results from {}"
    #                                .format(node_name))
    #             else:
    #                 logger.warning("Client {} banned from this task"
    #                                .format(node_name))
    #
    #             return self.ExtraData(should_wait=should_wait)
    #
    #         if self.get_progress == 1.0:
    #             logger.error("Task already computed")
    #             return self.ExtraData()
    #
    #         query_extra_data_func(self, perf_index, num_cores, node_id, node_name)
    #     return accepting_qed

def accepting(query_extra_data_func):
    """
    A decorator for query_extra_data - it wraps the function with verification code
    :param query_extra_data:
    :return:
    """
    def accepting_qed(self, perf_index: float, num_cores=1, node_id: str = None,
                         node_name: str = None) -> Task.ExtraData:
        verdict = self._accept_client(node_id)
        if verdict != AcceptClientVerdict.ACCEPTED:

            should_wait = verdict == AcceptClientVerdict.SHOULD_WAIT
            if should_wait:
                logger.warning("Waiting for results from {}"
                               .format(node_name))
            else:
                logger.warning("Client {} banned from this task"
                               .format(node_name))

            return self.ExtraData(should_wait=should_wait)

        if self.get_progress == 1.0:
            logger.error("Task already computed")
            return self.ExtraData()

        return query_extra_data_func(self, perf_index, num_cores, node_id, node_name)
    return accepting_qed


class CoreTaskBuilder(TaskBuilder):
    TASK_CLASS = CoreTask

    def __init__(self, node_name, task_definition, root_path, dir_manager):
        super(CoreTaskBuilder, self).__init__()
        self.task_definition = task_definition
        self.node_name = node_name
        self.root_path = root_path
        self.dir_manager = dir_manager
        self.src_code = ""
        self.environment = None

    def build(self):
        task = self.TASK_CLASS(**self.get_task_kwargs())
        task.initialize(self.dir_manager)
        return task

    def get_task_kwargs(self, **kwargs):
        kwargs["task_definition"] = self.task_definition
        kwargs["node_name"] = self.node_name
        kwargs["root_path"] = self.root_path
        return kwargs

    @classmethod
    def build_minimal_definition(cls, task_type, dictionary):
        definition = task_type.definition()
        definition.options = task_type.options()
        definition.task_id = dictionary.get('id', str(uuid.uuid4()))
        definition.task_type = task_type.name
        definition.resources = set(dictionary['resources'])
        definition.total_subtasks = int(dictionary['subtasks'])
        definition.main_program_file = task_type.defaults.main_program_file

        # FIXME: Backward compatibility only. Remove after upgrading GUI.
        definition.legacy = dictionary.get('legacy', False)

        return definition

    @classmethod
    def build_definition(cls, task_type, dictionary, minimal=False):
        if not minimal:
            definition = cls.build_full_definition(task_type, dictionary)
        else:
            definition = cls.build_minimal_definition(task_type, dictionary)

        definition.add_to_resources()
        return definition

    @classmethod
    def build_full_definition(cls, task_type, dictionary):
        definition = cls.build_minimal_definition(task_type, dictionary)
        definition.task_name = dictionary['name']
        definition.max_price = float(dictionary['bid']) * denoms.ether

        definition.full_task_timeout = string_to_timeout(
            dictionary['timeout'])
        definition.subtask_timeout = string_to_timeout(
            dictionary['subtask_timeout'])
        definition.output_file = cls.get_output_path(dictionary, definition)

        return definition

    @classmethod
    def build_dictionary(cls, definition):
        task_timeout = timeout_to_string(definition.full_task_timeout)
        subtask_timeout = timeout_to_string(definition.subtask_timeout)
        output_path = cls.build_output_path(definition)

        return {
            'id': to_unicode(definition.task_id),
            'type': to_unicode(definition.task_type),
            'name': to_unicode(definition.task_name),
            'timeout': to_unicode(task_timeout),
            'subtask_timeout': to_unicode(subtask_timeout),
            'subtasks': definition.total_subtasks,
            'bid': float(definition.max_price) / denoms.ether,
            'resources': [to_unicode(r) for r in definition.resources],
            'options': {
                'output_path': to_unicode(output_path)
            },
            # FIXME: Backward compatibility only. Remove after upgrading GUI.
            'legacy': definition.legacy,
        }

    @classmethod
    def get_output_path(cls, dictionary, definition):
        options = dictionary['options']

        # FIXME: Backward compatibility only. Remove after upgrading GUI.
        if definition.legacy:
            return options['output_path']

        return os.path.join(options['output_path'], definition.task_name)

    @staticmethod
    def build_output_path(definition):
        # FIXME: Backward compatibility only. Remove after upgrading GUI.
        if definition.legacy:
            return definition.output_file

        return definition.output_file.rsplit(os.path.sep, 1)[0]
