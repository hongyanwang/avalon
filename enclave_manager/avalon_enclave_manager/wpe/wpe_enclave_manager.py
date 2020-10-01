#!/usr/bin/env python3

# Copyright 2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import logging
import hashlib
import os
import sys

import avalon_enclave_manager.sgx_work_order_request as work_order_request
import avalon_enclave_manager.wpe.wpe_enclave as enclave
import avalon_enclave_manager.wpe.wpe_enclave_info as enclave_info
from avalon_enclave_manager.base_enclave_manager import EnclaveManager
from avalon_enclave_manager.wpe.wpe_requester import WPERequester
from error_code.error_status import WorkOrderStatus
from avalon_enclave_manager.work_order_processor_manager \
    import WOProcessorManager
from avalon_enclave_manager.enclave_type import EnclaveType

logger = logging.getLogger(__name__)


class WorkOrderProcessorEnclaveManager(WOProcessorManager):
    """
    Manager class to handle work order processing in a worker
    pool setup
    """

    def __init__(self, config):

        super().__init__(config)
        # Calculate sha256 of enclave id to get 32 bytes. Then take a
        # hexdigest for hex str.
        enclave_id_utf = self.enclave_id.encode("UTF-8")
        self._identity = hashlib.sha256(enclave_id_utf).hexdigest()

# -------------------------------------------------------------------------

    def _create_signup_data(self):
        """
        Create WPE signup data.

        Returns :
            signup_data - Relevant signup data to be used for requests to the
                          enclave
        """
        # Instantiate enclaveinfo & initialize enclave in the process
        signup_data = enclave_info.WorkOrderProcessorEnclaveInfo(
            self._config, EnclaveType.WPE)
        self._wpe_requester = WPERequester(self._config)

        signup_cpp_obj = enclave.SignupInfoWPE()
        # Generate a nonce in trusted code
        verification_key_nonce = signup_cpp_obj.GenerateNonce(32)
        logger.info("Nonce generated by requester WPE : %s",
                    verification_key_nonce)
        response = self._wpe_requester.get_unique_verification_key(
            verification_key_nonce)
        if response is None:
            logger.error("Failed to get Unique ID from KME")
            return None
        # Received response contains result,verification_key and
        # verification_key_signature delimited by ' '
        self._unique_verification_key = response.split(' ')[1]
        self._unique_verification_key_signature = response.split(' ')[2]
        # Verify unique verification key signature using unique id
        result = signup_cpp_obj.VerifyUniqueIdSignature(
            self._unique_verification_key,
            self._unique_verification_key_signature)
        if result != 0:
            logger.error("Failed to verify unique id signature")
            return None
        # signup enclave
        signup_data.create_enclave_signup_data(self._unique_verification_key)
        # return signup data
        logger.info("WPE signup data {}".format(signup_data.proof_data))
        return signup_data

# -------------------------------------------------------------------------

    def _manager_on_boot(self):
        """
        Executes Boot flow of enclave manager
        """

        # Extended_measurements is a tuple, viz., basename and measurement
        # for the enclave
        _, mr_enclave = self.extended_measurements
        if self._wpe_requester\
            .register_wo_processor(self._unique_verification_key,
                                   self.encryption_key,
                                   self.proof_data,
                                   mr_enclave):
            logger.info("WPE registration successful")
            # Update mapping of worker_id to workers in a pool
            self._worker_kv_delegate.update_worker_map(
                self._worker_id, self._identity)
        else:
            logger.error("WPE registration failed. Cannot proceed further.")
            sys.exit(1)

# -------------------------------------------------------------------------

    def _execute_wo_in_trusted_enclave(self, input_json_str):
        """
        Submits workorder request to Worker enclave and retrieves the response

        Parameters :
            input_json_str - A JSON formatted str of the request to execute
        Returns :
            json_response - A JSON response received from the enclave. Errors
                            are also wrapped in a JSON str if exceptions have
                            occurred.
        """
        try:
            pre_proc_output = self._wpe_requester\
                .preprocess_work_order(input_json_str, self.encryption_key)
            if "error" in pre_proc_output:
                # If error in preprocessing response, skip workorder processing
                logger.error("Failed to preprocess at WPE enclave manager.")
                return pre_proc_output

            wo_request = work_order_request.SgxWorkOrderRequest(
                EnclaveType.WPE,
                input_json_str,
                pre_proc_output)
            wo_response = wo_request.execute()
        except Exception as e:
            logger.error("failed to execute work order; %s", str(e))
            wo_response = dict()
            wo_response["error"] = dict()
            wo_response["error"]["code"] = WorkOrderStatus.FAILED
            wo_response["error"]["message"] = str(e)
            logger.info("unknown enclave type response = %s", wo_response)
        return wo_response

# -------------------------------------------------------------------------


def main(args=None):
    import config.config as pconfig
    import utility.logger as plogger

    # parse out the configuration file first
    tcf_home = os.environ.get("TCF_HOME", "../../../../")

    conf_files = ["wpe_config.toml"]
    conf_paths = [".", tcf_home + "/"+"config"]

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="configuration file", nargs="+")
    parser.add_argument("--config-dir", help="configuration folder", nargs="+")
    parser.add_argument("--kme_listener_url",
                        help="KME listener url for requests to KME",
                        type=str)
    parser.add_argument(
        "--worker_id", help="Id of worker in plain text", type=str)

    (options, remainder) = parser.parse_known_args(args)

    if options.config:
        conf_files = options.config

    if options.config_dir:
        conf_paths = options.config_dir

    try:
        config = pconfig.parse_configuration_files(conf_files, conf_paths)
        json.dumps(config, indent=4)
    except pconfig.ConfigurationException as e:
        logger.error(str(e))
        sys.exit(-1)

    if options.kme_listener_url:
        config["KMEListener"]["kme_listener_url"] = options.kme_listener_url
    if options.worker_id:
        config["WorkerConfig"]["worker_id"] = options.worker_id

    plogger.setup_loggers(config.get("Logging", {}))
    sys.stdout = plogger.stream_to_logger(
        logging.getLogger("STDOUT"), logging.DEBUG)
    sys.stderr = plogger.stream_to_logger(
        logging.getLogger("STDERR"), logging.WARN)

    try:
        EnclaveManager.parse_command_line(config, remainder)
        logger.info("Initialize WorkOrderProcessor enclave_manager")
        enclave_manager = WorkOrderProcessorEnclaveManager(config)
        logger.info("About to start WorkOrderProcessor Enclave manager")
        enclave_manager.start_enclave_manager()
    except Exception as e:
        logger.error("Exception occurred while running WPE, " +
                     "exiting WPE enclave manager")
        exit(1)


main()
