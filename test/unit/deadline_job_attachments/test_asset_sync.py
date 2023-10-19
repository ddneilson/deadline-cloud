# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the Asset Synching class for task-level attachments."""

import json
from logging import getLogger
import shutil
from math import trunc
from pathlib import Path
from typing import Optional
from unittest.mock import ANY, MagicMock, call, mock_open, patch

import boto3
from deadline.job_attachments.progress_tracker import ProgressStatus
import pytest
from moto import mock_sts

import deadline
from deadline.job_attachments.asset_sync import AssetSync
from deadline.job_attachments.download import _progress_logger
from deadline.job_attachments.exceptions import Fus3ExecutableMissingError
from deadline.job_attachments.models import (
    Attachments,
    Job,
    JobAttachmentS3Settings,
    ManifestProperties,
    PathFormat,
    Queue,
)
from deadline.job_attachments.progress_tracker import (
    DownloadSummaryStatistics,
    SummaryStatistics,
)
from deadline.job_attachments._utils import _human_readable_file_size

from deadline.job_attachments.asset_manifests.decode import decode_manifest


class TestAssetSync:
    @pytest.fixture(autouse=True)
    def before_test(
        self,
        request,
        create_s3_bucket,
        default_job_attachment_s3_settings: JobAttachmentS3Settings,
        default_asset_sync: AssetSync,
    ):
        """
        Setup the default queue and s3 bucket for all asset tests.
        Mark test with `no_setup` if you don't want this setup to run.
        """
        if "no_setup" in request.keywords:
            return

        create_s3_bucket(bucket_name=default_job_attachment_s3_settings.s3BucketName)
        self.default_asset_sync = default_asset_sync

    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def asset_sync(self, farm_id: str, client: MagicMock) -> AssetSync:
        asset_sync = AssetSync(farm_id)
        asset_sync.s3_uploader._s3 = client
        return asset_sync

    def test_progress_logger_one_file(self) -> None:
        """
        Asserts that task runs are getting updated with the appropriate progress
        when only one file is being uploaded.
        """
        # GIVEN
        mock_progress_tracker_callback = MagicMock()
        callback = _progress_logger(
            file_size_in_bytes=10,
            progress_tracker_callback=mock_progress_tracker_callback,
        )

        # WHEN
        for _ in range(1, 11):
            callback(1)

        # THEN
        assert mock_progress_tracker_callback.call_count == 10
        calls = [call(1, False) for _ in range(1, 10)]
        calls.append(call(1, True))
        mock_progress_tracker_callback.assert_has_calls(calls)

    @pytest.mark.parametrize(
        ("file_size", "expected_output"),
        [
            (1000000000000000000, "1000.0 PB"),
            (89234597823492938, "89.23 PB"),
            (1000000000000001, "1.0 PB"),
            (1000000000000000, "1.0 PB"),
            (999999999999999, "1.0 PB"),
            (999995000000000, "1.0 PB"),
            (999994000000000, "999.99 TB"),
            (8934587945678, "8.93 TB"),
            (1000000000001, "1.0 TB"),
            (1000000000000, "1.0 TB"),
            (999999999999, "1.0 TB"),
            (999995000000, "1.0 TB"),
            (999994000000, "999.99 GB"),
            (83748237582, "83.75 GB"),
            (1000000001, "1.0 GB"),
            (1000000000, "1.0 GB"),
            (999999999, "1.0 GB"),
            (999995000, "1.0 GB"),
            (999994000, "999.99 MB"),
            (500229150, "500.23 MB"),
            (1000001, "1.0 MB"),
            (1000000, "1.0 MB"),
            (999999, "1.0 MB"),
            (999995, "1.0 MB"),
            (999994, "999.99 KB"),
            (96771, "96.77 KB"),
            (1001, "1.0 KB"),
            (1000, "1.0 KB"),
            (999, "999.0 B"),
            (934, "934.0 B"),
        ],
    )
    def test_human_readable_file_size(self, file_size: int, expected_output: str) -> None:
        """
        Test that given a file size in bytes, the expected human readable file size is output.
        """
        assert _human_readable_file_size(file_size) == expected_output

    def test_sync_inputs_no_inputs_successful(
        self,
        tmp_path: Path,
        default_queue: Queue,
        default_job: Job,
        attachments_no_inputs: Attachments,
    ):
        """Asserts that sync_inputs is successful when no required assets exist for the Job"""
        # GIVEN
        default_job.attachments = attachments_no_inputs
        session_dir = str(tmp_path)
        dest_dir = "assetroot-27bggh78dd2b568ab123"
        local_root = str(Path(session_dir) / dest_dir)

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.download_files_from_manifests",
            side_effect=[DownloadSummaryStatistics()],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            side_effect=[dest_dir],
        ):
            mock_on_downloading_files = MagicMock(return_value=True)

            (summary_statistics, result_pathmap_rules) = self.default_asset_sync.sync_inputs(
                default_queue.jobAttachmentSettings,
                attachments_no_inputs,
                default_queue.queueId,
                default_job.jobId,
                tmp_path,
                on_downloading_files=mock_on_downloading_files,
            )

            # THEN
            expected_source_path_format = (
                "windows"
                if default_job.attachments.manifests[0].rootPathFormat == PathFormat.WINDOWS
                else "posix"
            )
            assert result_pathmap_rules == [
                {
                    "source_path_format": expected_source_path_format,
                    "source_path": default_job.attachments.manifests[0].rootPath,
                    "destination_path": local_root,
                }
            ]

            expected_summary_statistics = SummaryStatistics(
                total_time=summary_statistics.total_time,
                total_files=0,
                total_bytes=0,
                processed_files=0,
                processed_bytes=0,
                skipped_files=0,
                skipped_bytes=0,
                transfer_rate=0.0,
            )
            assert summary_statistics == expected_summary_statistics

    @pytest.mark.parametrize(
        ("job_fixture_name"),
        [
            ("default_job"),
            ("vfs_job"),
        ],
    )
    @pytest.mark.parametrize(
        ("s3_settings_fixture_name"),
        [
            ("default_job_attachment_s3_settings"),
        ],
    )
    def test_sync_inputs_successful(
        self,
        tmp_path: Path,
        default_queue: Queue,
        job_fixture_name: str,
        s3_settings_fixture_name: str,
        request: pytest.FixtureRequest,
    ):
        """Asserts that a valid manifest can be processed to download attachments from S3"""
        # GIVEN
        job: Job = request.getfixturevalue(job_fixture_name)
        s3_settings: JobAttachmentS3Settings = request.getfixturevalue(s3_settings_fixture_name)
        default_queue.jobAttachmentSettings = s3_settings
        session_dir = str(tmp_path)
        dest_dir = "assetroot-27bggh78dd2b568ab123"
        local_root = str(Path(session_dir) / dest_dir)
        assert job.attachments

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_manifest_from_s3",
            side_effect=[f"{local_root}/manifest.json"],
        ), patch("builtins.open", mock_open(read_data="test_manifest_file")), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.decode_manifest",
            side_effect=["test_manifest_data"],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.download_files_from_manifests",
            side_effect=[DownloadSummaryStatistics()],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            side_effect=[dest_dir],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.mount_vfs_from_manifests"
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.Fus3ProcessManager.find_fus3"
        ):
            mock_on_downloading_files = MagicMock(return_value=True)

            (_, result_pathmap_rules) = self.default_asset_sync.sync_inputs(
                s3_settings,
                job.attachments,
                default_queue.queueId,
                job.jobId,
                tmp_path,
                on_downloading_files=mock_on_downloading_files,
            )

            # THEN
            expected_source_path_format = (
                "windows"
                if job.attachments.manifests[0].rootPathFormat == PathFormat.WINDOWS
                else "posix"
            )
            assert result_pathmap_rules == [
                {
                    "source_path_format": expected_source_path_format,
                    "source_path": job.attachments.manifests[0].rootPath,
                    "destination_path": local_root,
                }
            ]

    @pytest.mark.parametrize(
        ("s3_settings_fixture_name"),
        [
            ("default_job_attachment_s3_settings"),
        ],
    )
    def test_sync_inputs_with_step_dependencies(
        self,
        tmp_path: Path,
        default_queue: Queue,
        default_job: Job,
        s3_settings_fixture_name: str,
        request: pytest.FixtureRequest,
    ):
        """Asserts that input syncing is done correctly when step dependencies are provided."""
        # GIVEN
        s3_settings: JobAttachmentS3Settings = request.getfixturevalue(s3_settings_fixture_name)
        default_queue.jobAttachmentSettings = s3_settings
        session_dir = str(tmp_path)
        dest_dir = "assetroot-27bggh78dd2b568ab123"
        local_root = str(Path(session_dir) / dest_dir)
        assert default_job.attachments

        step_output_root = "/home/outputs_roots"
        step_dest_dir = "assetroot-8a7d189e9c17186fb88b"

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_manifest_from_s3",
            side_effect=[f"{local_root}/manifest.json"],
        ), patch("builtins.open", mock_open(read_data="test_manifest_file")), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.decode_manifest",
            side_effect=["test_manifest_data"],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.download_files_from_manifests",
            side_effect=[DownloadSummaryStatistics()],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            side_effect=[dest_dir, step_dest_dir],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_output_manifests_by_asset_root",
            side_effect=[{step_output_root: {}}],
        ):
            mock_on_downloading_files = MagicMock(return_value=True)

            (_, result_pathmap_rules) = self.default_asset_sync.sync_inputs(
                s3_settings,
                default_job.attachments,
                default_queue.queueId,
                default_job.jobId,
                tmp_path,
                step_dependencies=["step-1"],
                on_downloading_files=mock_on_downloading_files,
            )

            # THEN
            expected_source_path_format = (
                "windows"
                if default_job.attachments.manifests[0].rootPathFormat == PathFormat.WINDOWS
                else "posix"
            )
            assert result_pathmap_rules == [
                {
                    "source_path_format": expected_source_path_format,
                    "source_path": default_job.attachments.manifests[0].rootPath,
                    "destination_path": local_root,
                },
            ]

    @pytest.mark.parametrize(
        ("s3_settings_fixture_name"),
        [
            ("default_job_attachment_s3_settings"),
        ],
    )
    def test_sync_inputs_with_step_dependencies_same_root_vfs_on_posix(
        self,
        tmp_path: Path,
        default_queue: Queue,
        vfs_job: Job,
        s3_settings_fixture_name: str,
        test_manifest_one: dict,
        test_manifest_two: dict,
        request: pytest.FixtureRequest,
    ):
        """Asserts that input syncing is done correctly when step dependencies are provided."""
        # GIVEN
        job = vfs_job
        s3_settings: JobAttachmentS3Settings = request.getfixturevalue(s3_settings_fixture_name)
        default_queue.jobAttachmentSettings = s3_settings
        session_dir = str(tmp_path)
        dest_dir = "assetroot-27bggh78dd2b568ab123"
        local_root = str(Path(session_dir) / dest_dir)
        assert job.attachments

        test_manifest = decode_manifest(json.dumps(test_manifest_two))

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_manifest_from_s3",
            side_effect=[f"{local_root}/manifest.json"],
        ), patch("builtins.open", mock_open(read_data=json.dumps(test_manifest_one))), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.download_files_from_manifests",
            side_effect=[DownloadSummaryStatistics()],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            return_value=dest_dir,
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_output_manifests_by_asset_root",
            return_value={"tmp/": [(test_manifest, "hello")]},
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.merge_asset_manifests",
        ) as merge_manifests_mock, patch(
            f"{deadline.__package__}.job_attachments.download.write_manifest_to_temp_file",
            return_value="tmp_manifest",
        ), patch(
            "sys.platform", "linux"
        ):
            mock_on_downloading_files = MagicMock(return_value=True)

            (_, result_pathmap_rules) = self.default_asset_sync.sync_inputs(
                s3_settings,
                job.attachments,
                default_queue.queueId,
                job.jobId,
                tmp_path,
                step_dependencies=["step-1"],
                on_downloading_files=mock_on_downloading_files,
            )

            # THEN
            merge_manifests_mock.assert_called()
            expected_source_path_format = (
                "windows"
                if job.attachments.manifests[0].rootPathFormat == PathFormat.WINDOWS
                else "posix"
            )

            assert result_pathmap_rules == [
                {
                    "source_path_format": expected_source_path_format,
                    "source_path": job.attachments.manifests[0].rootPath,
                    "destination_path": local_root,
                },
            ]

    @mock_sts
    @pytest.mark.parametrize(
        (
            "s3_settings_fixture_name",
            "attachments_fixture_name",
            "expected_cas_prefix",
            "expected_output_prefix",
        ),
        [
            (
                "default_job_attachment_s3_settings",
                "default_attachments",
                "assetRoot/Data/",
                "assetRoot/Manifests/farm-1234567890abcdefghijklmnopqrstuv/queue-01234567890123456789012345678901/job-01234567890123456789012345678901/test_step4/test_task4/2023-07-13T14:35:26.123456Z_session-action-1/",
            ),
            (
                "default_job_attachment_s3_settings",
                "windows_attachments",
                "assetRoot/Data/",
                "assetRoot/Manifests/farm-1234567890abcdefghijklmnopqrstuv/queue-01234567890123456789012345678901/job-01234567890123456789012345678901/test_step4/test_task4/2023-07-13T14:35:26.123456Z_session-action-1/",
            ),
        ],
    )
    def test_sync_outputs(
        self,
        tmp_path: Path,
        default_queue: Queue,
        default_job: Job,
        session_action_id: str,
        s3_settings_fixture_name: str,
        attachments_fixture_name: str,
        expected_cas_prefix: str,
        expected_output_prefix: str,
        request: pytest.FixtureRequest,
        assert_expected_files_on_s3,
        assert_canonical_manifest,
    ):
        """
        Test that output files get uploaded to the CAS, skipping upload for files that are already in the CAS,
        and tests that an output manifest is uploaded to the Output prefix.
        """
        # GIVEN
        s3_settings: JobAttachmentS3Settings = request.getfixturevalue(s3_settings_fixture_name)
        attachments: Attachments = request.getfixturevalue(attachments_fixture_name)
        default_queue.jobAttachmentSettings = s3_settings
        default_job.attachments = attachments
        root_path = str(tmp_path)
        local_root = Path(f"{root_path}/assetroot-15addf56bb1a568df964")
        test_step = "test_step4"
        test_task = "test_task4"

        expected_output_root = Path(local_root).joinpath("test/outputs")
        expected_file_path = Path(expected_output_root).joinpath("test.txt")
        expected_sub_file_path = Path(expected_output_root).joinpath("inner_dir/test2.txt")

        expected_file_rel_path = "test/outputs/test.txt"
        expected_sub_file_rel_path = "test/outputs/inner_dir/test2.txt"

        # Add the files to S3
        s3 = boto3.Session(region_name="us-west-2").resource("s3")  # pylint: disable=invalid-name
        bucket = s3.Bucket(s3_settings.s3BucketName)
        bucket.put_object(
            Key=f"{expected_cas_prefix}hash1",
            Body="a",
        )
        expected_metadata = s3.meta.client.head_object(
            Bucket=s3_settings.s3BucketName, Key=f"{expected_cas_prefix}hash1"
        )

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync._hash_file",
            side_effect=["hash1", "hash2"],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._hash_data", side_effect=["hash3"]
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            side_effect=[local_root],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._float_to_iso_datetime_string",
            side_effect=["2023-07-13T14:35:26.123456Z"],
        ):
            mock_on_uploading_files = MagicMock(return_value=True)

            try:
                # Need to test having multiple files and subdirectories with files
                Path(expected_file_path).parent.mkdir(parents=True, exist_ok=True)
                with open(expected_file_path, "w") as test_file:
                    test_file.write("Test Output\n")
                Path(expected_sub_file_path).parent.mkdir(parents=True, exist_ok=True)
                with open(expected_sub_file_path, "w") as test_file:
                    test_file.write("Test Sub-Output\n")

                expected_processed_bytes = expected_sub_file_path.resolve().stat().st_size
                expected_skipped_bytes = expected_file_path.resolve().stat().st_size
                expected_total_bytes = expected_processed_bytes + expected_skipped_bytes
                expected_file_mtime = trunc(expected_file_path.stat().st_mtime_ns // 1000)
                expected_sub_file_mtime = trunc(expected_sub_file_path.stat().st_mtime_ns // 1000)

                # Actually run the test
                summary_statistics = self.default_asset_sync.sync_outputs(
                    s3_settings=s3_settings,
                    attachments=attachments,
                    queue_id=default_queue.queueId,
                    job_id=default_job.jobId,
                    step_id=test_step,
                    task_id=test_task,
                    session_action_id=session_action_id,
                    start_time=1234.56,
                    session_dir=tmp_path,
                    on_uploading_files=mock_on_uploading_files,
                )
            finally:
                # Need to clean up after
                if local_root.exists():
                    shutil.rmtree(local_root)

            # THEN
            actual_metadata = s3.meta.client.head_object(
                Bucket=s3_settings.s3BucketName, Key=f"{expected_cas_prefix}hash1"
            )
            assert actual_metadata["LastModified"] == expected_metadata["LastModified"]
            assert_expected_files_on_s3(
                bucket,
                expected_files={
                    f"{expected_cas_prefix}hash1",
                    f"{expected_cas_prefix}hash2",
                    f"{expected_output_prefix}hash3_output.xxh128",
                },
            )

            assert_canonical_manifest(
                bucket,
                f"{expected_output_prefix}hash3_output.xxh128",
                expected_manifest='{"hashAlg":"xxh128","manifestVersion":"2023-03-03",'
                f'"paths":[{{"hash":"hash2","mtime":{expected_sub_file_mtime},"path":"{expected_sub_file_rel_path}",'
                f'"size":{expected_processed_bytes}}},'
                f'{{"hash":"hash1","mtime":{expected_file_mtime},"path":"{expected_file_rel_path}",'
                f'"size":{expected_skipped_bytes}}}],'
                f'"totalSize":{expected_total_bytes}}}',
            )

            readable_total_input_bytes = _human_readable_file_size(expected_total_bytes)

            expected_summary_statistics = SummaryStatistics(
                total_time=summary_statistics.total_time,
                total_files=2,
                total_bytes=expected_total_bytes,
                processed_files=1,
                processed_bytes=expected_processed_bytes,
                skipped_files=1,
                skipped_bytes=expected_skipped_bytes,
                transfer_rate=expected_processed_bytes / summary_statistics.total_time,
            )

            actual_args, _ = mock_on_uploading_files.call_args
            actual_last_progress_report = actual_args[0]
            assert actual_last_progress_report.status == ProgressStatus.UPLOAD_IN_PROGRESS
            assert actual_last_progress_report.progress == 100.0
            assert (
                f"Uploaded {readable_total_input_bytes} / {readable_total_input_bytes} of 2 files (Transfer rate: "
                in actual_last_progress_report.progressMessage
            )

            assert summary_statistics == expected_summary_statistics

    @pytest.mark.parametrize(
        ("job", "expected_settings"),
        [(Job(jobId="job-98765567890123456789012345678901"), None), (None, None)],
    )
    def test_get_attachments_not_found_return_none(
        self, job: Job, expected_settings: Optional[Attachments]
    ):
        """Tests that get_attachments returns the expected result if Job or settings are None"""
        with patch(f"{deadline.__package__}.job_attachments.asset_sync.get_job", side_effect=[job]):
            actual = self.default_asset_sync.get_attachments("test-farm", "test-queue", "test-job")
            assert actual == expected_settings

    def test_get_attachments_successful(
        self, default_job: Job, default_attachments: Optional[Attachments]
    ):
        """Tests that get_attachments returns the expected result"""
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_job", side_effect=[default_job]
        ):
            actual = self.default_asset_sync.get_attachments(
                "test-farm", "test-queue", default_job.jobId
            )
            assert actual == default_attachments

    @pytest.mark.parametrize(
        ("queue", "expected_settings"),
        [
            (
                Queue(
                    queueId="queue-98765567890123456789012345678901",
                    displayName="test-queue",
                    farmId="test-farm",
                    status="test",
                ),
                None,
            ),
            (None, None),
        ],
    )
    def test_get_s3_settings_not_found_return_none(
        self, queue: Queue, expected_settings: Optional[JobAttachmentS3Settings]
    ):
        """Tests that get_s3_settings returns the expected result if Queue or S3 settings are None"""
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_queue", side_effect=[queue]
        ):
            actual = self.default_asset_sync.get_s3_settings("test-farm", "test-queue")
            assert actual == expected_settings

    def test_get_s3_settings_successful(
        self,
        default_queue: Queue,
        default_job_attachment_s3_settings: Optional[JobAttachmentS3Settings],
    ):
        """Tests that get_s3_settings returns the expected result"""
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_queue",
            side_effect=[default_queue],
        ):
            actual = self.default_asset_sync.get_s3_settings("test-farm", default_queue.queueId)
            assert actual == default_job_attachment_s3_settings

    def test_sync_inputs_with_storage_profiles_path_mapping_rules(
        self,
        default_queue: Queue,
        default_job: Job,
        tmp_path: Path,
    ):
        """Tests when a non-empty `storage_profiles_path_mapping_rules` is passed to `sync_inputs`.
        Check that, for input manifests with an `fileSystemLocationName`, if the root path
        corresponding to it exists in the `storage_profiles_path_mapping_rules`, the download
        is attempted to the correct destination path."""
        # GIVEN
        default_job.attachments = Attachments(
            manifests=[
                ManifestProperties(
                    rootPath="/tmp",
                    rootPathFormat=PathFormat.POSIX,
                    inputManifestPath="manifest_input.xxh128",
                    inputManifestHash="manifesthash",
                    outputRelativeDirectories=["test/outputs"],
                ),
                ManifestProperties(
                    fileSystemLocationName="Movie 1",
                    rootPath="/home/user/movie1",
                    rootPathFormat=PathFormat.POSIX,
                    inputManifestPath="manifest-movie1_input.xxh128",
                    inputManifestHash="manifestmovie1hash",
                    outputRelativeDirectories=["test/outputs"],
                ),
            ],
        )
        dest_dir = "assetroot-27bggh78dd2b568ab123"
        local_root = str(tmp_path.joinpath(dest_dir))

        storage_profiles_path_mapping_rules = {
            "/home/user/movie1": "/tmp/movie1",
        }

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_manifest_from_s3",
            side_effect=[
                f"{local_root}/manifest_input.xxh128",
                f"{local_root}/manifest-movie1_input.xxh128",
            ],
        ), patch("builtins.open", mock_open(read_data="test_manifest_file")), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.decode_manifest",
            return_value="test_manifest_data",
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.download_files_from_manifests",
            return_value=DownloadSummaryStatistics(),
        ) as mock_download_files_from_manifests, patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            side_effect=[dest_dir],
        ):
            mock_on_downloading_files = MagicMock(return_value=True)

            (summary_statistics, result_pathmap_rules) = self.default_asset_sync.sync_inputs(
                s3_settings=default_queue.jobAttachmentSettings,
                attachments=default_job.attachments,
                queue_id=default_queue.queueId,
                job_id=default_job.jobId,
                session_dir=tmp_path,
                storage_profiles_path_mapping_rules=storage_profiles_path_mapping_rules,
                on_downloading_files=mock_on_downloading_files,
            )

            # THEN
            assert result_pathmap_rules == [
                {
                    "source_path_format": "posix",
                    "source_path": default_job.attachments.manifests[0].rootPath,
                    "destination_path": local_root,
                }
            ]

            mock_download_files_from_manifests.assert_called_once_with(
                s3_bucket="test-bucket",
                manifests_by_root={
                    f"{local_root}": "test_manifest_data",
                    "/tmp/movie1": "test_manifest_data",
                },
                cas_prefix="assetRoot/Data",
                fs_permission_settings=None,
                session=ANY,
                on_downloading_files=mock_on_downloading_files,
                logger=getLogger("deadline.job_attachments"),
            )

    @pytest.mark.parametrize(
        ("job_fixture_name"),
        [
            ("default_job"),
            ("vfs_job"),
        ],
    )
    @pytest.mark.parametrize(
        ("s3_settings_fixture_name"),
        [
            ("default_job_attachment_s3_settings"),
        ],
    )
    def test_sync_inputs_successful_using_vfs_fallback(
        self,
        tmp_path: Path,
        default_queue: Queue,
        job_fixture_name: str,
        s3_settings_fixture_name: str,
        request: pytest.FixtureRequest,
    ):
        """Asserts that a valid manifest can be processed to download attachments from S3"""
        # GIVEN
        job: Job = request.getfixturevalue(job_fixture_name)
        s3_settings: JobAttachmentS3Settings = request.getfixturevalue(s3_settings_fixture_name)
        default_queue.jobAttachmentSettings = s3_settings
        session_dir = str(tmp_path)
        dest_dir = "assetroot-27bggh78dd2b568ab123"
        local_root = str(Path(session_dir) / dest_dir)
        assert job.attachments

        # WHEN
        with patch(
            f"{deadline.__package__}.job_attachments.asset_sync.get_manifest_from_s3",
            side_effect=[f"{local_root}/manifest.json"],
        ), patch("builtins.open", mock_open(read_data="test_manifest_file")), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.decode_manifest",
            side_effect=["test_manifest_data"],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.download_files_from_manifests",
            side_effect=[DownloadSummaryStatistics()],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync._get_unique_dest_dir_name",
            side_effect=[dest_dir],
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.Fus3ProcessManager.find_fus3",
            side_effect=Fus3ExecutableMissingError,
        ), patch(
            f"{deadline.__package__}.job_attachments.asset_sync.mount_vfs_from_manifests"
        ) as mock_mount_vfs, patch(
            "sys.platform", "linux"
        ):
            mock_on_downloading_files = MagicMock(return_value=True)

            (_, result_pathmap_rules) = self.default_asset_sync.sync_inputs(
                s3_settings,
                job.attachments,
                default_queue.queueId,
                job.jobId,
                tmp_path,
                on_downloading_files=mock_on_downloading_files,
            )

            # THEN
            expected_source_path_format = (
                "windows"
                if job.attachments.manifests[0].rootPathFormat == PathFormat.WINDOWS
                else "posix"
            )
            assert result_pathmap_rules == [
                {
                    "source_path_format": expected_source_path_format,
                    "source_path": job.attachments.manifests[0].rootPath,
                    "destination_path": local_root,
                }
            ]
            mock_mount_vfs.assert_not_called()