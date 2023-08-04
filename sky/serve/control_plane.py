"""Control Plane: the central control plane of SkyServe.

Responsible for autoscaling and replica management.
"""
import argparse
import fastapi
import logging
from typing import Optional
import uvicorn

from sky import serve
from sky.serve import autoscalers
from sky.serve import infra_providers

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-6s | %(name)-40s || %(message)s',
    datefmt='%m-%d %H:%M:%S',
    force=True)
logger = logging.getLogger(__name__)


class ControlPlane:
    """Control Plane: control everything about replica.

    This class is responsible for:
        - Starting and terminating the replica monitor and autoscaler.
        - Providing the HTTP Server API for SkyServe to communicate with.
    """

    def __init__(self,
                 port: int,
                 infra_provider: infra_providers.InfraProvider,
                 autoscaler: Optional[autoscalers.Autoscaler] = None) -> None:
        self.port = port
        self.infra_provider = infra_provider
        self.autoscaler = autoscaler
        self.app = fastapi.FastAPI()

    # TODO(tian): Authentication!!!
    def run(self) -> None:

        @self.app.post('/control_plane/get_num_requests')
        async def get_num_requests(request: fastapi.Request):
            # await request
            request_data = await request.json()
            # get request data
            num_requests = request_data['num_requests']
            logger.info(f'Received request: {request_data}')
            if isinstance(self.autoscaler, autoscalers.RequestRateAutoscaler):
                self.autoscaler.set_num_requests(num_requests)
            return {'message': 'Success'}

        @self.app.get('/control_plane/get_autoscaler_query_interval')
        def get_autoscaler_query_interval():
            if isinstance(self.autoscaler, autoscalers.RequestRateAutoscaler):
                return {'query_interval': self.autoscaler.get_query_interval()}
            return {'query_interval': None}

        @self.app.get('/control_plane/get_ready_replicas')
        def get_ready_replicas():
            return {'ready_replicas': self.infra_provider.get_ready_replicas()}

        @self.app.get('/control_plane/get_replica_info')
        def get_replica_info():
            return {'replica_info': self.infra_provider.get_replica_info()}

        @self.app.get('/control_plane/get_replica_nums')
        def get_replica_nums():
            return {
                'num_ready_replicas': self.infra_provider.ready_replica_num(),
                'num_unhealthy_replicas':
                    self.infra_provider.unhealthy_replica_num(),
                'num_failed_replicas': self.infra_provider.failed_replica_num()
            }

        @self.app.post('/control_plane/terminate')
        def terminate(request: fastapi.Request):
            del request
            # request_data = request.json()
            # TODO(tian): Authentication!!!
            logger.info('Terminating service...')
            self.infra_provider.terminate_replica_fetcher()
            if self.autoscaler is not None:
                self.autoscaler.terminate_monitor()
            msg = self.infra_provider.terminate()
            return {'message': msg}

        # Run replica_monitor and autoscaler.monitor (if autoscaler is defined)
        # in separate threads in the background.
        # This should not block the main thread.
        self.infra_provider.start_replica_fetcher()
        if self.autoscaler is not None:
            self.autoscaler.start_monitor()

        logger.info(
            f'SkyServe Control Plane started on http://0.0.0.0:{self.port}')
        uvicorn.run(self.app, host='0.0.0.0', port=self.port)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SkyServe Control Plane')
    parser.add_argument('--service-name',
                        type=str,
                        help='Name of the service',
                        required=True)
    parser.add_argument('--task-yaml',
                        type=str,
                        help='Task YAML file',
                        required=True)
    parser.add_argument('--port',
                        '-p',
                        type=int,
                        help='Port to run the control plane',
                        required=True)
    args = parser.parse_args()

    # ======= Infra Provider =========
    service_spec = serve.SkyServiceSpec.from_yaml(args.task_yaml)
    _infra_provider = infra_providers.SkyPilotInfraProvider(
        args.task_yaml,
        args.service_name,
        readiness_path=service_spec.readiness_path,
        readiness_timeout=service_spec.readiness_timeout,
        post_data=service_spec.post_data)

    # ======= Autoscaler =========
    _autoscaler = autoscalers.RequestRateAutoscaler(
        _infra_provider,
        frequency=20,
        min_nodes=service_spec.min_replica,
        max_nodes=service_spec.max_replica,
        upper_threshold=service_spec.qps_upper_threshold,
        lower_threshold=service_spec.qps_lower_threshold,
        cooldown=60,
        query_interval=60)

    # ======= ControlPlane =========
    control_plane = ControlPlane(args.port, _infra_provider, _autoscaler)
    control_plane.run()