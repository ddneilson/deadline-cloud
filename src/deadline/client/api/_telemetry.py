# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import atexit
import json
import logging
import platform
import uuid
import random
import time

from configparser import ConfigParser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from queue import Queue, Full
from threading import Thread
from typing import Any, Dict, Optional
from urllib import request, error

from ...job_attachments.progress_tracker import SummaryStatistics

from ._session import get_studio_id, get_user_and_identity_store_id
from ..config import config_file
from .. import version

__cached_telemetry_client = None

logger = logging.getLogger(__name__)


@dataclass
class TelemetryEvent:
    """Base class for telemetry events"""

    event_type: str = "com.amazon.rum.deadline.uncategorized"
    event_details: Dict[str, Any] = field(default_factory=dict)


class TelemetryClient:
    """
    Sends telemetry events periodically to the Deadline Cloud telemetry service.

    This client holds a queue of events which is written to synchronously, and processed
    asynchronously, where events are sent in the background, so that it does not slow
    down user interactivity.

    Telemetry events contain non-personally-identifiable information that helps us
    understand how users interact with our software so we know what features our
    customers use, and/or what existing pain points are.

    Data is aggregated across a session ID (a UUID created at runtime), used to mark every
    telemetry event for the lifetime of the application), and a 'telemetry identifier' (a
    UUID recorded in the configuration file), to aggregate data across multiple application
    lifetimes on the same machine.

    Telemetry collection can be opted-out of by running:
    'deadline config set "telemetry.opt_out" true'
    """

    # Used for backing off requests if we encounter errors from the service.
    # See https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    MAX_QUEUE_SIZE = 25
    BASE_TIME = 0.5
    MAX_BACKOFF_SECONDS = 10  # The maximum amount of time to wait between retries
    MAX_RETRY_ATTEMPTS = 4

    def __init__(
        self,
        package_name: str,
        package_ver: str,
        config: Optional[ConfigParser] = None,
    ):
        self.telemetry_opted_out = config_file.str2bool(
            config_file.get_setting("telemetry.opt_out", config=config)
        )
        if self.telemetry_opted_out:
            return
        self.package_name = package_name
        self.package_ver = ".".join(package_ver.split(".")[:3])
        self.endpoint: str = f"{config_file.get_setting('settings.deadline_endpoint_url', config=config)}/2023-10-12/telemetry"

        # IDs for this session
        self.session_id: str = str(uuid.uuid4())
        self.telemetry_id: str = self._get_telemetry_identifier(config=config)
        # Get common data we'll include in each request
        self.system_metadata = self._get_system_metadata(config=config)

        self._start_threads()

    def _get_telemetry_identifier(self, config: Optional[ConfigParser] = None):
        identifier = config_file.get_setting("telemetry.identifier", config=config)
        try:
            uuid.UUID(identifier, version=4)
        except ValueError:  # Thrown if the user_id isn't in UUID4 format
            identifier = str(uuid.uuid4())
            config_file.set_setting("telemetry.identifier", identifier)
        return identifier

    def _start_threads(self) -> None:
        """Set up background threads for shutdown checking and request sending"""
        self.event_queue: Queue[Optional[TelemetryEvent]] = Queue(
            maxsize=TelemetryClient.MAX_QUEUE_SIZE
        )
        atexit.register(self._exit_cleanly)
        self.processing_thread: Thread = Thread(
            target=self._process_event_queue_thread, daemon=True
        )
        self.processing_thread.start()

    def _get_system_metadata(self, config: Optional[ConfigParser]) -> Dict[str, Any]:
        """
        Builds up a dict of non-identifiable metadata about the system environment.

        This will be used in the Rum event metadata, which has a limit of 10 unique values.
        """
        platform_info = platform.uname()
        metadata: Dict[str, Any] = {
            "service": self.package_name,
            "version": self.package_ver,
            "python_version": platform.python_version(),
            "osName": "macOS" if platform_info.system == "Darwin" else platform_info.system,
            "osVersion": platform_info.release,
        }

        user_id, _ = get_user_and_identity_store_id(config=config)
        if user_id:
            metadata["user_id"] = user_id
        studio_id: Optional[str] = get_studio_id(config=config)
        if studio_id:
            metadata["studio_id"] = studio_id

        return metadata

    def _exit_cleanly(self):
        self.event_queue.put(None)
        self.processing_thread.join()

    def _send_request(self, req: request.Request) -> None:
        attempts = 0
        success = False
        while not success:
            try:
                logger.warning(f"Sending telemetry data: {req.data}")
                with request.urlopen(req):
                    logger.debug("Successfully sent telemetry.")
                    success = True
            except error.HTTPError as httpe:
                if httpe.code == 429 or httpe.code == 500:
                    logger.debug(f"Error received from service. Waiting to retry: {str(httpe)}")

                    attempts += 1
                    if attempts >= TelemetryClient.MAX_RETRY_ATTEMPTS:
                        raise Exception("Max retries reached sending telemetry")

                    backoff_sleep = random.uniform(
                        0,
                        min(
                            TelemetryClient.MAX_BACKOFF_SECONDS,
                            TelemetryClient.BASE_TIME * 2**attempts,
                        ),
                    )
                    time.sleep(backoff_sleep)
                else:  # Reraise any exceptions we didn't expect
                    raise

    def _process_event_queue_thread(self):
        """Background thread for processing the telemetry event data queue and sending telemetry requests."""
        while True:
            # Blocks until we get a new entry in the queue
            event_data: Optional[TelemetryEvent] = self.event_queue.get()
            # We've received the shutdown signal
            if event_data is None:
                return

            headers = {"Accept": "application-json", "Content-Type": "application-json"}
            request_body = {
                "BatchId": str(uuid.uuid4()),
                "RumEvents": [
                    {
                        "details": str(json.dumps(event_data.event_details)),
                        "id": str(uuid.uuid4()),
                        "metadata": str(json.dumps(self.system_metadata)),
                        "timestamp": int(datetime.now().timestamp()),
                        "type": event_data.event_type,
                    },
                ],
                "UserDetails": {"sessionId": self.session_id, "userId": self.telemetry_id},
            }
            request_body_encoded = str(json.dumps(request_body)).encode("utf-8")
            req = request.Request(url=self.endpoint, data=request_body_encoded, headers=headers)
            try:
                self._send_request(req)
            except Exception:
                # Silently swallow any kind of uncaught exception and stop sending telemetry
                return
            self.event_queue.task_done()

    def _put_telemetry_record(self, event: TelemetryEvent) -> None:
        if self.telemetry_opted_out:
            return
        try:
            self.event_queue.put_nowait(event)
        except Full:
            # Silently swallow the error if the event queue is full (due to throttling of the service)
            pass

    def _record_summary_statistics(
        self, event_type: str, summary: SummaryStatistics, from_gui: bool
    ):
        details: Dict[str, Any] = asdict(summary)
        details["usage_mode"] = "GUI" if from_gui else "CLI"
        self._put_telemetry_record(TelemetryEvent(event_type=event_type, event_details=details))

    def record_hashing_summary(self, summary: SummaryStatistics, *, from_gui: bool = False):
        self._record_summary_statistics(
            "com.amazon.rum.deadline.job_attachments.hashing_summary", summary, from_gui
        )

    def record_upload_summary(self, summary: SummaryStatistics, *, from_gui: bool = False):
        self._record_summary_statistics(
            "com.amazon.rum.deadline.job_attachments.upload_summary", summary, from_gui
        )

    def record_error(self, event_details: Dict[str, Any], exception_type: str):
        event_details["exception_type"] = exception_type
        # Possiblity to add stack trace here
        self.record_event("com.amazon.rum.deadline.error", event_details)

    def record_event(self, event_type: str, event_details: Dict[str, Any]):
        self._put_telemetry_record(
            TelemetryEvent(
                event_type=event_type,
                event_details=event_details,
            )
        )


def get_telemetry_client(
    package_name: str, package_ver: str, config: Optional[ConfigParser] = None
) -> TelemetryClient:
    """
    Retrieves the cached telemetry client, lazy-loading the first time this is called.
    :param config: Optional configuration to use for the client. Loads defaults if not given.
    :param package_name: Optional override package name to include in requests. Defaults to the 'deadline-cloud' package.
    :param package_ver: Optional override package version to include in requests. Defaults to the 'deadline-cloud' version.
    :return: Telemetry client to make requests with.
    """
    global __cached_telemetry_client
    if not __cached_telemetry_client:
        __cached_telemetry_client = TelemetryClient(
            package_name=package_name,
            package_ver=package_ver,
            config=config,
        )

    return __cached_telemetry_client


def get_deadline_cloud_library_telemetry_client(
    config: Optional[ConfigParser] = None,
) -> TelemetryClient:
    """
    Retrieves the cached telemetry client, specifying the Deadline Cloud Client Library's package information.
    :param config: Optional configuration to use for the client. Loads defaults if not given.
    :return: Telemetry client to make requests with.
    """
    return get_telemetry_client("deadline-cloud-library", version, config=config)