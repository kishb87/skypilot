"""Autoscalers: perform autoscaling by monitoring metrics."""
import bisect
import dataclasses
import enum
import time
import typing
from typing import Any, Callable, Dict, List, Optional, Union

from sky import sky_logging
from sky.serve import constants
from sky.serve import serve_state
from sky.serve import solvers
from sky.serve.serve_utils import AcceleratorType
from sky.utils import env_options

if typing.TYPE_CHECKING:
    from sky.serve import replica_managers
    from sky.serve import service_spec

logger = sky_logging.init_logger(__name__)


class AutoscalerDecisionOperator(enum.Enum):
    SCALE_UP = 'scale_up'
    SCALE_DOWN = 'scale_down'


@dataclasses.dataclass
class AutoscalerDecision:
    """Autoscaling decisions.

    |---------------------------------------------------------------|
    | Operator   | TargetType       | Meaning                       |
    |------------|------------------|-------------------------------|
    | SCALE_UP   | Dict[str, Any]   | Resource override to add      |
    |------------|------------------|-------------------------------|
    | SCALE_DOWN | int              | Replica id to remove          |
    |---------------------------------------------------------------|
    """
    operator: AutoscalerDecisionOperator
    #target: Optional[Union[int, List[int]]]
    target: Union[Optional[Dict[str, Any]], int, str]

    def __repr__(self) -> str:
        return f'AutoscalerDecision({self.operator}, {self.target})'


class Autoscaler:
    """Abstract class for autoscalers."""

    def __init__(self, spec: 'service_spec.SkyServiceSpec',
                 frequency: int) -> None:
        """Initialize the autoscaler.

        Variables:
            min_replicas: Minimum number of replicas.
            max_replicas: Maximum number of replicas. Default to fixed
                number of replicas, i.e. min_replicas == max_replicas.
            frequency: Frequency of autoscaling in seconds.
        """
        self.min_replicas: int = spec.min_replicas
        self.max_replicas: int = spec.max_replicas or spec.min_replicas
        self.frequency = frequency
        if self.frequency < constants.LB_CONTROLLER_SYNC_INTERVAL_SECONDS:
            logger.warning('Autoscaler frequency is less than '
                           'controller sync interval. It might '
                           'not always got the latest information.')

    def collect_request_information(
            self, request_aggregator_info: Dict[str, Any]) -> None:
        """Collect request information from aggregator for autoscaling."""
        raise NotImplementedError

    def evaluate_scaling(
        self,
        replica_infos: List['replica_managers.ReplicaInfo'],
    ) -> AutoscalerDecision:
        """Evaluate autoscale options based on replica information."""
        raise NotImplementedError


class RequestRateAutoscaler(Autoscaler):
    """RequestRateAutoscaler: Autoscale according to request rate.

    Scales when the number of requests in the given interval is above or below
    the threshold.
    """

    def __init__(self, spec: 'service_spec.SkyServiceSpec', frequency: int,
                 cooldown: int, rps_window_size: int) -> None:
        """Initialize the request rate autoscaler.

        Variables:
            upper_threshold: Upper threshold for scale up. If None, no scale up.
            lower_threshold: Lower threshold for scale down. If None, no scale
                down.
            cooldown: Cooldown between two scaling operations in seconds.
            rps_window_size: Window size for rps calculating.
            last_scale_operation: Time of last scale operation.
            request_timestamps: All request timestamps within the window.
        """
        super().__init__(spec, frequency)
        self.upper_threshold: Optional[float] = spec.qps_upper_threshold
        self.lower_threshold: Optional[float] = spec.qps_lower_threshold
        self.cooldown: int = cooldown
        self.rps_window_size: int = rps_window_size
        self.last_scale_operation: float = 0.
        self.request_timestamps: List[float] = []

    def collect_request_information(
            self, request_aggregator_info: Dict[str, Any]) -> None:
        """Collect request information from aggregator for autoscaling.

        request_aggregator_info should be a dict with the following format:

        {
            'timestamps': [timestamp1 (float), timestamp2 (float), ...]
        }
        """
        self.request_timestamps.extend(
            request_aggregator_info.get('timestamps', []))
        current_time = time.time()
        index = bisect.bisect_left(self.request_timestamps,
                                   current_time - self.rps_window_size)
        self.request_timestamps = self.request_timestamps[index:]

    def evaluate_scaling(
        self,
        replica_infos: List['replica_managers.ReplicaInfo'],
    ) -> AutoscalerDecision:
        current_time = time.time()
        num_replicas = len(replica_infos)

        # Check if cooldown period has passed since the last scaling operation.
        # Only cooldown if bootstrapping is done.
        if num_replicas >= self.min_replicas:
            if current_time - self.last_scale_operation < self.cooldown:
                logger.info(
                    f'Current time: {current_time}, '
                    f'last scale operation: {self.last_scale_operation}, '
                    f'cooldown: {self.cooldown}')
                logger.info('Cooldown period has not passed since last scaling '
                            'operation. Skipping scaling.')
                return AutoscalerDecision(AutoscalerDecisionOperator.NO_OP,
                                          target=None)

        # Convert to requests per second.
        num_requests_per_second = len(
            self.request_timestamps) / self.rps_window_size
        # Edge case: num_replicas is zero.
        requests_per_replica = (num_requests_per_second / num_replicas
                                if num_replicas else num_requests_per_second)

        logger.info(f'Requests per replica: {requests_per_replica}')

        logger.info(f'Number of replicas: {num_replicas}')
        target_num_replicas = num_replicas
        if num_replicas < self.min_replicas:
            target_num_replicas = self.min_replicas
        elif (self.upper_threshold is not None and
              requests_per_replica > self.upper_threshold):
            scale_target = requests_per_replica / self.upper_threshold
            target_num_replicas = int(scale_target * num_replicas)
        elif (self.lower_threshold is not None and
              requests_per_replica < self.lower_threshold):
            scale_target = requests_per_replica / self.lower_threshold
            target_num_replicas = int(scale_target * num_replicas)

        target_num_replicas = max(self.min_replicas,
                                  min(self.max_replicas, target_num_replicas))
        num_replicas_delta = target_num_replicas - num_replicas
        if num_replicas_delta == 0:
            logger.info('No scaling needed.')
            return AutoscalerDecision(AutoscalerDecisionOperator.NO_OP,
                                      target=None)
        elif num_replicas_delta > 0:
            logger.info(f'Scaling up by {num_replicas_delta} replicas.')
            return AutoscalerDecision(AutoscalerDecisionOperator.SCALE_UP,
                                      target=num_replicas_delta)
        else:
            num_replicas_to_remove = -num_replicas_delta
            # Remove FAILED replicas first.
            replica_ids_to_remove: List[int] = []
            for info in replica_infos:
                if len(replica_ids_to_remove) >= num_replicas_to_remove:
                    break
                if info.status == serve_state.ReplicaStatus.FAILED:
                    replica_ids_to_remove.append(info.replica_id)
            # Then rest of them.
            for info in replica_infos:
                if len(replica_ids_to_remove) >= num_replicas_to_remove:
                    break
                replica_ids_to_remove.append(info.replica_id)
            logger.info(f'Scaling down by {num_replicas_to_remove} replicas '
                        f'(id: {replica_ids_to_remove}).')
            return AutoscalerDecision(AutoscalerDecisionOperator.SCALE_DOWN,
                                      target=replica_ids_to_remove)


class HeteroGPUAutoscaler(Autoscaler):
    """RequestRateAutoscaler: Autoscale according to request rate.

    Scales when the number of requests in the given interval is above or below
    the threshold.
    """

    def __init__(self, spec: 'service_spec.SkyServiceSpec', frequency: int,
                 cooldown: int, rps_window_size: int) -> None:
        """Initialize the request rate autoscaler.

        Variables:
            upper_threshold: Upper threshold for scale up. If None, no scale up.
            lower_threshold: Lower threshold for scale down. If None, no scale
                down.
            cooldown: Cooldown between two scaling operations in seconds.
            rps_window_size: Window size for rps calculating.
            last_scale_operation: Time of last scale operation.
            request_timestamps: All request timestamps within the window.
        """
        super().__init__(spec, frequency)
        self.upper_threshold: Optional[float] = spec.qps_upper_threshold
        self.lower_threshold: Optional[float] = spec.qps_lower_threshold
        self.cooldown: int = cooldown
        self.rps_window_size: int = 300
        self.last_scale_operation: float = 0.
        self.request_timestamps: List[float] = []
        self.request_timestamps_distribution: List[List[float]] = [[], [], [],
                                                                   [], [], [],
                                                                   []]
        self.request_distribution: List[int] = [0, 0, 0, 0, 0, 0, 0]
        self.request_rate_dist: List[float] = [0, 0, 0, 0, 0, 0, 0]
        self.total_request_in_window = 0

    def collect_request_information(
            self, request_aggregator_info: Dict[str, Any]) -> None:
        """Collect request information from aggregator for autoscaling.

        request_aggregator_info should be a dict with the following format:

        {
            'timestamps': [timestamp1 (float), timestamp2 (float), ...]
        }
        """
        self.total_request_in_window = 0
        timestamps_from_loadbalancer = request_aggregator_info.get(
            'timestamps', [[], [], [], [], [], [], []])
        current_time = time.time()
        for idx, lst in enumerate(self.request_timestamps_distribution):
            lst.extend(timestamps_from_loadbalancer[idx])
            index = bisect.bisect_left(lst, current_time - self.rps_window_size)
            lst = lst[index:]
            self.total_request_in_window += len(lst)

        if self.total_request_in_window != 0:
            for idx, lst in enumerate(self.request_timestamps_distribution):
                self.request_distribution[idx] = len(
                    lst) / self.total_request_in_window
                self.request_rate_dist[idx] = len(lst) / self.rps_window_size

        print(
            f'autoscaler.collect_request_information(timestamps_from_loadbalancer): {timestamps_from_loadbalancer}'
        )
        print(
            f'autoscaler.collect_request_information(self.request_timestamps_distribution): {self.request_timestamps_distribution}'
        )
        print(
            f'autoscaler.collect_request_information(self.total_request_in_window): {self.total_request_in_window}'
        )
        print(
            f'autoscaler.collect_request_information(self.request_distribution): {self.request_distribution}'
        )

    def _get_accelerator_override_dict(
            self, instance_type: AcceleratorType) -> Dict[str, Any]:
        return {'accelerators': f'{instance_type.value}:1'}

    def _get_autoscaler_decision(
            self,
            operator: AutoscalerDecisionOperator,
            accelerator: Optional[AcceleratorType] = None,
            is_primary: Optional[bool] = None,
            replica_id: Optional[int] = None) -> AutoscalerDecision:
        if operator == AutoscalerDecisionOperator.SCALE_UP:
            assert accelerator, accelerator
            assert is_primary, is_primary
            operator_target = self._get_accelerator_override_dict(accelerator)
            operator_target.update({
                'is_primary': True if is_primary else False,
                'is_fallback': False if is_primary else True,
            })
            decision = AutoscalerDecision(operator, target=operator_target)
        else:  # SCALE_DOWN
            assert replica_id, replica_id
            decision = AutoscalerDecision(operator, target=replica_id)
        return decision

    def _get_fallback_allocation(self, accelerator: AcceleratorType):
        if accelerator == AcceleratorType.A10:
            num, fallback_type = 0, None
        elif accelerator == AcceleratorType.A100:
            num, fallback_type = 4, AcceleratorType.A10
        return num, fallback_type

    def fallback_scale_down_sync(
        self,
        service_name: str,
        replica_manager: 'replica_managers.ReplicaManager',
    ) -> None:
        replica_infos = serve_state.get_replica_infos(service_name)
        replica_info_dicts = [
            info.to_info_dict(
                with_handle=env_options.Options.SHOW_DEBUG_INFO.get())
            for info in replica_infos
        ]
        logger.info(
            f'All replica info before fallback sync: {replica_info_dicts}')
        # Iterate through replica_infos and check for primary replicas
        # Check if each primary replica in state of ReplicaStatus.READY
        # has existing fallback replicas
        ready_primary_replica_infos = [
            info for info in replica_infos if info.is_primary and
            info.is_ready and info.fallback_replica_id_list
        ]
        for info in ready_primary_replica_infos:
            fallback_replica_id_to_terminate = info.fallback_replica_id_list
            for replica_id in fallback_replica_id_to_terminate:
                replica_manager.scale_down(replica_id)
                info.fallback_replica_id_list.remove(replica_id)
                serve_state.add_or_update_replica(service_name, info.replica_id,
                                                  info)

    def evaluate_scaling(
        self,
        replica_infos: List['replica_managers.ReplicaInfo'],
    ) -> List[AutoscalerDecision]:
        # get replica infos and retrieve number of GPU types currently running
        alive_replica_infos = [info for info in replica_infos if info.is_alive]

        all_replica_infos_to_scale_down: List[
            'replica_managers.ReplicaInfo'] = []
        scaling_decisions: (List[Union[AutoscalerDecision,
                                       List[AutoscalerDecision]]]) = []

        def _get_replica_infos_to_scale_down(
            info_filter: Callable[['replica_managers.ReplicaInfo'], bool],
            status_order: List['serve_state.ReplicaStatus'],
            num_limit: int,
        ) -> List[int]:
            replica_infos_to_scale_down: List[int] = []
            for target_status in status_order:
                for info in alive_replica_infos:
                    if info_filter(info) and info.status == target_status:
                        if len(replica_infos_to_scale_down) >= num_limit:
                            return replica_infos_to_scale_down
                        replica_infos_to_scale_down.append(info)
            for info in alive_replica_infos:
                if info_filter(info) and info.status not in status_order:
                    if len(replica_infos_to_scale_down) >= num_limit:
                        return replica_infos_to_scale_down
                    replica_infos_to_scale_down.append(info)
            return replica_infos_to_scale_down

        # Pass the histogram to solver and get the ideal GPU allocation. Assume
        # the allocation to be a dictionary in a form of
        # {replica_manager.AcceleratorType.A100: # of A100s needed,
        #  replica_manager.AcceleratorType.A10: # of A10s needed}
        accel_allocation = solvers.IlpSolver(self.request_rate_dist)
        # Compare the nubmers from GPU allocation and replica infos to get what needs to be scaled up/down
        # return a list of AutoscalerDecisions
        for accelerator in [AcceleratorType.A10, AcceleratorType.A100]:
            num_alive_accel = len([
                info for info in alive_replica_infos
                if info.accelerator == accelerator
            ])
            if accelerator in accel_allocation:
                diff_accel_num = num_alive_accel - accel_allocation[accelerator]
                # Need to scale up
                if diff_accel_num < 0:
                    num_to_scale_up = abs(diff_accel_num)
                    for _ in range(num_to_scale_up):
                        num, fallback_type = self._get_fallback_allocation(
                            accelerator)
                        # Setting up to launch fallback replicas along with
                        # primary replica
                        if num > 0:
                            primary_fallback_decisions = []
                            for _ in range(num):
                                fallback_decision = self._get_autoscaler_decision(
                                    AutoscalerDecisionOperator.SCALE_UP,
                                    accelerator=fallback_type,
                                    is_primary=False)
                                primary_fallback_decisions.append(
                                    fallback_decision)
                            primary_decision = self._get_autoscaler_decision(
                                AutoscalerDecisionOperator.SCALE_UP,
                                accelerator=accelerator,
                                is_primary=True)
                            primary_fallback_decisions.append(primary_decision)
                            scaling_decisions.append(primary_fallback_decisions)
                        # There is no fallback replica to be launched for the
                        # accelerator type
                        else:
                            decision = self._get_autoscaler_decision(
                                AutoscalerDecisionOperator.SCALE_UP,
                                accelerator=accelerator,
                                is_primary=True)
                            scaling_decisions.append(decision)
                # Need to scale down
                elif diff_accel_num > 0:
                    # Need to make sure the fallback replicas are not included
                    # in the scale down list.
                    all_replica_infos_to_scale_down.extend(
                        _get_replica_infos_to_scale_down(
                            info_filter=lambda info: info.accelerator ==
                            accelerator and info.is_primary,
                            status_order=serve_state.ReplicaStatus.
                            scale_down_decision_order(),
                            num_limit=diff_accel_num,
                        ))

        # Need to make sure to put down the fallback replicas
        # if the primary replica is set to be down.
        for info in all_replica_infos_to_scale_down:
            decision = self._get_autoscaler_decision(
                AutoscalerDecisionOperator.SCALE_DOWN,
                replica_id=info.replica_id)
            scaling_decisions.append(decision)
            for replica_id in info.fallback_replica_id_list:
                decision = self._get_autoscaler_decision(
                    AutoscalerDecisionOperator.SCALE_DOWN,
                    replica_id=replica_id)
                scaling_decisions.append(decision)

        if not scaling_decisions:
            logger.info('No scaling needed.')

        return scaling_decisions
