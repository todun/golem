from pathlib import Path
import re
import sys
import tempfile
import time
import traceback
import typing

from twisted.internet import reactor, task
from twisted.internet.error import ReactorNotRunning
from twisted.internet import _sslverify  # pylint: disable=protected-access

from scripts.node_integration_tests.rpc.client import RPCClient
from scripts.node_integration_tests import helpers, tasks

from golem.rpc.cert import CertificateError

if typing.TYPE_CHECKING:
    from .test_config_base import TestConfigBase, NodeConfig


_sslverify.platformTrust = lambda: None


class NodeTestPlaybook:
    INTERVAL = 1

    start_time = None

    _loop = None

    nodes_root: typing.Optional[Path] = None
    provider_node = None
    requestor_node = None
    provider_output_queue = None
    requestor_output_queue = None

    provider_port = None

    exit_code = None
    current_step = 0
    provider_key = None
    requestor_key = None
    known_tasks = None
    task_id = None
    started = False
    nodes_started = False
    task_in_creation = False
    output_path = None
    subtasks = None

    reconnect_attempts_left = 7
    reconnect_countdown_initial = 10
    reconnect_countdown = None

    playbook_description = 'Runs a golem node integration test'

    node_restart_count = 0

    @property
    def task_settings_dict(self) -> dict:
        return tasks.get_settings(self.config.task_settings)

    @property
    def output_extension(self):
        return self.task_settings_dict.get('options', {}).get('format')

    @property
    def current_step_method(self):
        try:
            return self.steps[self.current_step]
        except IndexError:
            return None

    @property
    def current_step_name(self) -> str:
        method = self.current_step_method
        return method.__name__ if method else ''

    @property
    def time_elapsed(self):
        return time.time() - self.start_time

    def fail(
            self,
            msg=None,
            dump_provider_output=False,
            dump_requestor_output=False):
        print(msg or "Test run failed after {} seconds on step {}: {}".format(
                self.time_elapsed, self.current_step, self.current_step_name))

        if self.config.dump_output_on_fail or dump_provider_output:
            helpers.print_output(self.provider_output_queue, 'PROVIDER ')
        if self.config.dump_output_on_fail or dump_requestor_output:
            helpers.print_output(self.requestor_output_queue, 'REQUESTOR ')

        self.stop(1)

    def success(self):
        print("Test run completed in {} seconds after {} steps.".format(
            self.time_elapsed, self.current_step + 1, ))
        self.stop(0)

    def next(self):
        self.current_step += 1

    def previous(self):
        assert (self.current_step > 0), "Cannot move back past step 0"
        self.current_step -= 1

    def print_result(self, result):
        print("Result: {}".format(result))

    def print_error(self, error):
        print("Error: {}".format(error))

    def _wait_gnt_eth(self, role, result):
        gnt_balance = helpers.to_ether(result.get('gnt'))
        gntb_balance = helpers.to_ether(result.get('av_gnt'))
        eth_balance = helpers.to_ether(result.get('eth'))
        if gnt_balance > 0 and eth_balance > 0 and gntb_balance > 0:
            print("{} has {} total GNT ({} GNTB) and {} ETH.".format(
                role.capitalize(), gnt_balance, gntb_balance, eth_balance))
            self.next()

        else:
            print("Waiting for {} GNT(B)/converted GNTB/ETH ({}/{}/{})".format(
                role.capitalize(), gnt_balance, gntb_balance, eth_balance))
            time.sleep(15)

    def step_wait_provider_gnt(self):
        def on_success(result):
            return self._wait_gnt_eth('provider', result)

        return self.call_provider('pay.balance', on_success=on_success)

    def step_wait_requestor_gnt(self):
        def on_success(result):
            return self._wait_gnt_eth('requestor', result)

        return self.call_requestor('pay.balance', on_success=on_success)

    def step_get_provider_key(self):
        def on_success(result):
            print("Provider key", result)
            self.provider_key = result
            self.next()

        def on_error(_):
            print("Waiting for the Provider node...")
            time.sleep(3)

        return self.call_provider('net.ident.key',
                             on_success=on_success, on_error=on_error)

    def step_get_requestor_key(self):
        def on_success(result):
            print("Requestor key", result)
            self.requestor_key = result
            self.next()

        def on_error(result):
            print("Waiting for the Requestor node...")
            time.sleep(3)

        return self.call_requestor('net.ident.key',
                              on_success=on_success, on_error=on_error)

    def step_configure_provider(self):
        provider_config = self.config.current_provider
        if provider_config is None:
            self.fail("provider node config is not defined")
            return

        if not provider_config.opts:
            self.next()
            return

        def on_success(_):
            print("Configured provider")
            self.next()

        def on_error(_):
            print("failed configuring provider")
            self.fail()

        return self.call_provider('env.opts.update', provider_config.opts,
                                  on_success=on_success, on_error=on_error)

    def step_configure_requestor(self):
        requestor_config = self.config.current_requestor
        if requestor_config is None:
            self.fail("requestor node config is not defined")
            return

        if not requestor_config.opts:
            self.next()
            return

        def on_success(_):
            print("Configured requestor")
            self.next()

        def on_error(_):
            print("failed configuring requestor")
            self.fail()

        return self.call_provider('env.opts.update', requestor_config.opts,
                                  on_success=on_success, on_error=on_error)

    def step_get_provider_network_info(self):
        def on_success(result):
            if result.get('listening') and result.get('port_statuses'):
                provider_ports = list(result.get('port_statuses').keys())
                self.provider_port = provider_ports[0]
                print("Provider's port: {} (all: {})".format(
                    self.provider_port, provider_ports))
                self.next()
            else:
                print("Waiting for Provider's network info...")
                time.sleep(3)

        return self.call_provider('net.status',
                             on_success=on_success, on_error=self.print_error)

    def step_ensure_requestor_network(self):
        def on_success(result):
            if result.get('listening') and result.get('port_statuses'):
                requestor_ports = list(result.get('port_statuses').keys())
                print("Requestor's listening on: {}".format(requestor_ports))
                self.next()
            else:
                print("Waiting for Requestor's network info...")
                time.sleep(3)

        return self.call_requestor('net.status',
                              on_success=on_success, on_error=self.print_error)

    def step_connect_nodes(self):
        def on_success(result):
            print("Peer connection initialized.")
            self.reconnect_countdown = self.reconnect_countdown_initial
            self.next()
        return self.call_requestor('net.peer.connect',
                              ("localhost", self.provider_port, ),
                              on_success=on_success)

    def step_verify_peer_connection(self):
        def on_success(result):
            if len(result) > 1:
                print("Too many peers")
                self.fail()
                return
            elif len(result) == 1:
                peer = result[0]
                if peer['key_id'] != self.provider_key:
                    print("Connected peer: {} != provider peer: {}",
                          peer.key, self.provider_key)
                    self.fail()
                    return

                print("Requestor connected with provider.")
                self.next()
            else:
                if self.reconnect_countdown <= 0:
                    if self.reconnect_attempts_left > 0:
                        self.reconnect_attempts_left -= 1
                        print("Retrying peer connection.")
                        self.previous()
                        return
                    else:
                        self.fail("Could not sync nodes despite trying hard.")
                        return
                else:
                    self.reconnect_countdown -= 1
                    print("Waiting for nodes to sync...")
                    time.sleep(10)

        return self.call_requestor('net.peers.connected',
                              on_success=on_success, on_error=self.print_error)

    def step_get_known_tasks(self):
        def on_success(result):
            self.known_tasks = set(map(lambda r: r['id'], result))
            print("Got current tasks list from the requestor.")
            self.next()

        return self.call_requestor('comp.tasks',
                              on_success=on_success, on_error=self.print_error)

    def step_create_task(self):
        print("Output path: {}".format(self.output_path))
        print("Task dict: {}".format(self.config.task_dict))

        def on_success(result):
            if result[0]:
                print("Created task.")
                self.task_in_creation = False
                self.next()
            else:
                msg = result[1]
                if re.match('Not enough GNT', msg):
                    print("Waiting for Requestor's GNTB...")
                    time.sleep(30)
                    self.task_in_creation = False
                else:
                    print("Failed to create task {}".format(msg))
                    self.fail()

        if not self.task_in_creation:
            self.task_in_creation = True
            return self.call_requestor('comp.task.create',
                                       self.config.task_dict,
                                  on_success=on_success,
                                  on_error=self.print_error)

    def step_get_task_id(self):

        def on_success(result):
            tasks = set(map(lambda r: r['id'], result))
            new_tasks = tasks - self.known_tasks
            if len(new_tasks) != 1:
                print("Cannot find the new task ({})".format(new_tasks))
                time.sleep(30)
            else:
                self.task_id = list(new_tasks)[0]
                print("Task id: {}".format(self.task_id))
                self.next()

        return self.call_requestor('comp.tasks',
                              on_success=on_success, on_error=self.print_error)

    def step_get_task_status(self):
        def on_success(result):
            print("Task status: {}".format(result['status']))
            self.next()

        return self.call_requestor('comp.task', self.task_id,
                              on_success=on_success, on_error=self.print_error)

    def step_wait_task_finished(self):
        def on_success(result):
            if result['status'] == 'Finished':
                print("Task finished.")
                self.next()
            elif result['status'] == 'Timeout':
                self.fail("Task timed out :( ... ")
            else:
                print("{} ... ".format(result['status']))
                time.sleep(10)

        return self.call_requestor('comp.task', self.task_id,
                       on_success=on_success, on_error=self.print_error)

    def step_verify_output(self):
        settings = self.task_settings_dict
        output_file_name = settings.get('name') + '.' + self.output_extension

        print("Verifying output file: {}".format(output_file_name))
        found_files = list(
            Path(self.output_path).glob(f'**/{output_file_name}')
        )

        if len(found_files) > 0 and found_files[0].is_file():
            print("Output present :)")
            self.next()
        else:
            print("Failed to find the output.")
            self.fail()

    def step_get_subtasks(self):
        def on_success(result):
            self.subtasks = [
                s.get('subtask_id')
                for s in result
                if s.get('status') == 'Finished'
            ]
            if not self.subtasks:
                self.fail("No subtasks found???")
            self.next()

        return self.call_requestor('comp.task.subtasks', self.task_id,
                              on_success=on_success, on_error=self.print_error)

    def step_verify_provider_income(self):
        def on_success(result):
            payments = [
                p.get('subtask')
                for p in result
                if p.get('payer') == self.requestor_key
            ]
            unpaid = set(self.subtasks) - set(payments)
            if unpaid:
                print("Found subtasks with no matching payments: %s" % unpaid)
                self.fail()
                return

            print("All subtasks accounted for.")
            self.success()

        return self.call_provider(
            'pay.incomes', on_success=on_success, on_error=self.print_error)

    def step_stop_nodes(self):
        if self.nodes_started:
            print("Stopping nodes")
            self.stop_nodes()

        time.sleep(10)
        provider_exit = self.provider_node.poll()
        requestor_exit = self.requestor_node.poll()
        if provider_exit is not None and requestor_exit is not None:
            if provider_exit or requestor_exit:
                print(
                    "Abnormal termination provider: %s, requestor: %s",
                    provider_exit,
                    requestor_exit,
                )
                self.fail()
            else:
                print("Stopped nodes")
                self.next()
        else:
            print("...")

    def step_restart_nodes(self):
        print("Starting nodes again")
        self.config.use_next_nodes()

        self.task_in_creation = False
        time.sleep(60)

        self.start_nodes()
        print("Nodes restarted")
        self.next()

    initial_steps: typing.Tuple = (
        step_get_provider_key,
        step_get_requestor_key,
        step_configure_provider,
        step_configure_requestor,
        step_get_provider_network_info,
        step_ensure_requestor_network,
        step_connect_nodes,
        step_verify_peer_connection,
        step_wait_provider_gnt,
        step_wait_requestor_gnt,
        step_get_known_tasks,
    )

    steps: typing.Tuple = initial_steps + (
        step_create_task,
        step_get_task_id,
        step_get_task_status,
        step_wait_task_finished,
        step_verify_output,
        step_get_subtasks,
        step_verify_provider_income,
    )

    @staticmethod
    def _call_rpc(method, *args, port, datadir, on_success, on_error, **kwargs):
        try:
            client = RPCClient(
                host='localhost',
                port=port,
                datadir=datadir,
            )
        except CertificateError as e:
            on_error(e)
            return

        return client.call(method, *args,
                           on_success=on_success,
                           on_error=on_error,
                           **kwargs)

    def call_requestor(self, method, *args,
                       on_success=lambda x: print(x),
                       on_error=lambda: None,
                       **kwargs):
        requestor_config = self.config.current_requestor
        if requestor_config is None:
            self.fail("requestor node config is not defined")
            return

        return self._call_rpc(
            method,
            port=requestor_config.rpc_port,
            datadir=requestor_config.datadir,
            *args,
            on_success=on_success,
            on_error=on_error,
            **kwargs,
        )

    def call_provider(self, method, *args,
                      on_success=lambda x: print(x),
                      on_error=None,
                      **kwargs):
        provider_config = self.config.current_provider
        if provider_config is None:
            self.fail("provider node config is not defined")
            return

        return self._call_rpc(
            method,
            port=provider_config.rpc_port,
            datadir=provider_config.datadir,
            *args,
            on_success=on_success,
            on_error=on_error,
            **kwargs,
        )

    def start_nodes(self):
        provider_config = self.config.current_provider
        if provider_config is not None:
            print("Provider config: {}".format(repr(provider_config)))
            self.provider_node = helpers.run_golem_node(
                provider_config.script,
                provider_config.make_args(),
                nodes_root=self.nodes_root,
            )
            self.provider_output_queue = helpers.get_output_queue(
                self.provider_node)

        requestor_config = self.config.current_requestor
        if requestor_config is not None:
            print("Requestor config: {}".format(repr(requestor_config)))
            self.requestor_node = helpers.run_golem_node(
                requestor_config.script,
                requestor_config.make_args(),
                nodes_root=self.nodes_root,
            )

            self.requestor_output_queue = helpers.get_output_queue(
                self.requestor_node)

        self.nodes_started = True

    def stop_nodes(self):
        if self.nodes_started:
            if self.provider_node:
                helpers.gracefully_shutdown(self.provider_node, 'Provider')
            if self.requestor_node:
                helpers.gracefully_shutdown(self.requestor_node, 'Requestor')
            self.nodes_started = False

    def run(self):
        if self.nodes_started:
            if self.provider_node:
                provider_exit = self.provider_node.poll()
                helpers.report_termination(provider_exit, "Provider")
                if provider_exit is not None:
                    self.fail(
                        "Provider exited abnormally.",
                        dump_provider_output=self.config.dump_output_on_crash,
                    )

            if self.requestor_node:
                requestor_exit = self.requestor_node.poll()
                helpers.report_termination(requestor_exit, "Requestor")
                if requestor_exit is not None:
                    self.fail(
                        "Requestor exited abnormally.",
                        dump_requestor_output=self.config.dump_output_on_crash,
                    )

        try:
            method = self.current_step_method
            if callable(method):
                return method(self)
            else:
                self.fail("Ran out of steps after step {}".format(
                    self.current_step))
                return
        except Exception as e:  # noqa pylint:disable=too-broad-exception
            e, msg, tb = sys.exc_info()
            print("Exception {}: {} on step {}: {}".format(
                e.__name__, msg, self.current_step, self.current_step_name))
            traceback.print_tb(tb)
            self.fail()
            return

    def __init__(self, config: 'TestConfigBase') -> None:
        self.config = config

        def setup_datadir(
                role: str,
                node_configs:
                'typing.Union[None, NodeConfig, typing.List[NodeConfig]]') \
                -> None:
            if node_configs is None:
                return
            if isinstance(node_configs, list):
                datadir: typing.Optional[str] = None
                for node_config in node_configs:
                    if node_config.datadir is None:
                        if datadir is None:
                            datadir = helpers.mkdatadir(role)
                        node_config.datadir = datadir
            else:
                if node_configs.datadir is None:
                    node_configs.datadir = helpers.mkdatadir(role)

        setup_datadir('requestor', config.requestor)
        setup_datadir('provider', config.provider)

        self.output_path = tempfile.mkdtemp(
            prefix="golem-integration-test-output-")
        helpers.set_task_output_path(self.config.task_dict, self.output_path)

        self.start_nodes()
        self.started = True

    @classmethod
    def start(cls: 'typing.Type[NodeTestPlaybook]', config: 'TestConfigBase') \
            -> 'NodeTestPlaybook':
        playbook = cls(config)
        playbook.start_time = time.time()
        playbook._loop = task.LoopingCall(playbook.run)
        d = playbook._loop.start(cls.INTERVAL, False)
        d.addErrback(lambda x: print(x))

        reactor.addSystemEventTrigger(
            'before', 'shutdown', lambda: playbook.stop(2))
        reactor.run()

        return playbook

    def stop(self, exit_code):
        if not self.started:
            return

        self.started = False
        try:
            reactor.stop()
        except ReactorNotRunning:
            pass

        self.stop_nodes()
        self.exit_code = exit_code
