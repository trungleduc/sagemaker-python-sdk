# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""This module stores JumpStart Model factory methods."""
from __future__ import absolute_import
import json


from typing import Any, Dict, List, Optional, Union
from sagemaker import environment_variables, image_uris, instance_types, model_uris, script_uris
from sagemaker.async_inference.async_inference_config import AsyncInferenceConfig
from sagemaker.base_deserializers import BaseDeserializer
from sagemaker.base_serializers import BaseSerializer
from sagemaker.explainer.explainer_config import ExplainerConfig
from sagemaker.jumpstart.artifacts import (
    _model_supports_inference_script_uri,
    _retrieve_model_init_kwargs,
    _retrieve_model_deploy_kwargs,
    _retrieve_model_package_arn,
)
from sagemaker.jumpstart.artifacts.resource_names import _retrieve_resource_name_base
from sagemaker.jumpstart.constants import (
    DEFAULT_JUMPSTART_SAGEMAKER_SESSION,
    INFERENCE_ENTRY_POINT_SCRIPT_NAME,
    JUMPSTART_DEFAULT_REGION_NAME,
    JUMPSTART_LOGGER,
)
from sagemaker.model_metrics import ModelMetrics
from sagemaker.metadata_properties import MetadataProperties
from sagemaker.drift_check_baselines import DriftCheckBaselines
from sagemaker.jumpstart.enums import JumpStartScriptScope
from sagemaker.jumpstart.types import (
    JumpStartModelDeployKwargs,
    JumpStartModelInitKwargs,
    JumpStartModelRegisterKwargs,
)
from sagemaker.jumpstart.utils import (
    add_jumpstart_model_id_version_tags,
    update_dict_if_key_not_present,
    resolve_model_sagemaker_config_field,
    verify_model_region_and_return_specs,
)

from sagemaker.model_monitor.data_capture_config import DataCaptureConfig
from sagemaker.base_predictor import Predictor
from sagemaker import accept_types, content_types, serializers, deserializers

from sagemaker.serverless.serverless_inference_config import ServerlessInferenceConfig
from sagemaker.session import Session
from sagemaker.utils import name_from_base
from sagemaker.workflow.entities import PipelineVariable
from sagemaker.compute_resource_requirements.resource_requirements import ResourceRequirements
from sagemaker import resource_requirements
from sagemaker.enums import EndpointType


def get_default_predictor(
    predictor: Predictor,
    model_id: str,
    model_version: str,
    region: str,
    tolerate_vulnerable_model: bool,
    tolerate_deprecated_model: bool,
    sagemaker_session: Session,
) -> Predictor:
    """Converts predictor returned from ``Model.deploy()`` into a JumpStart-specific one.

    Raises:
        RuntimeError: If a base-class predictor is not used.
    """

    # if there's a non-default predictor, do not mutate -- return as is
    if type(predictor) != Predictor:  # pylint: disable=C0123
        raise RuntimeError(
            "Can only get default predictor from base Predictor class. "
            f"Using Predictor class '{type(predictor).__name__}'."
        )

    predictor.serializer = serializers.retrieve_default(
        model_id=model_id,
        model_version=model_version,
        region=region,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        sagemaker_session=sagemaker_session,
    )
    predictor.deserializer = deserializers.retrieve_default(
        model_id=model_id,
        model_version=model_version,
        region=region,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        sagemaker_session=sagemaker_session,
    )
    predictor.accept = accept_types.retrieve_default(
        model_id=model_id,
        model_version=model_version,
        region=region,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        sagemaker_session=sagemaker_session,
    )
    predictor.content_type = content_types.retrieve_default(
        model_id=model_id,
        model_version=model_version,
        region=region,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        sagemaker_session=sagemaker_session,
    )

    return predictor


def _add_region_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets region kwargs based on default or override, returns full kwargs."""

    kwargs.region = kwargs.region or JUMPSTART_DEFAULT_REGION_NAME

    return kwargs


def _add_sagemaker_session_to_kwargs(
    kwargs: Union[JumpStartModelInitKwargs, JumpStartModelDeployKwargs]
) -> JumpStartModelInitKwargs:
    """Sets session in kwargs based on default or override, returns full kwargs."""
    kwargs.sagemaker_session = kwargs.sagemaker_session or DEFAULT_JUMPSTART_SAGEMAKER_SESSION
    return kwargs


def _add_role_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets role based on default or override, returns full kwargs."""

    kwargs.role = resolve_model_sagemaker_config_field(
        field_name="role",
        field_val=kwargs.role,
        sagemaker_session=kwargs.sagemaker_session,
        default_value=kwargs.role,
    )

    return kwargs


def _add_model_version_to_kwargs(
    kwargs: JumpStartModelInitKwargs,
) -> JumpStartModelInitKwargs:
    """Sets model version based on default or override, returns full kwargs."""

    kwargs.model_version = kwargs.model_version or "*"

    return kwargs


def _add_vulnerable_and_deprecated_status_to_kwargs(
    kwargs: JumpStartModelInitKwargs,
) -> JumpStartModelInitKwargs:
    """Sets deprecated and vulnerability check status, returns full kwargs."""

    kwargs.tolerate_deprecated_model = kwargs.tolerate_deprecated_model or False
    kwargs.tolerate_vulnerable_model = kwargs.tolerate_vulnerable_model or False

    return kwargs


def _add_instance_type_to_kwargs(
    kwargs: JumpStartModelInitKwargs, disable_instance_type_logging: bool = False
) -> JumpStartModelInitKwargs:
    """Sets instance type based on default or override, returns full kwargs."""

    orig_instance_type = kwargs.instance_type

    kwargs.instance_type = kwargs.instance_type or instance_types.retrieve_default(
        region=kwargs.region,
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        scope=JumpStartScriptScope.INFERENCE,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
        training_instance_type=kwargs.training_instance_type,
    )

    if not disable_instance_type_logging and orig_instance_type is None:
        JUMPSTART_LOGGER.info(
            "No instance type selected for inference hosting endpoint. Defaulting to %s.",
            kwargs.instance_type,
        )

    return kwargs


def _add_image_uri_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets image uri based on default or override, returns full kwargs."""

    kwargs.image_uri = kwargs.image_uri or image_uris.retrieve(
        region=kwargs.region,
        framework=None,
        image_scope=JumpStartScriptScope.INFERENCE,
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        instance_type=kwargs.instance_type,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    return kwargs


def _add_model_data_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets model data based on default or override, returns full kwargs."""

    model_data: Union[str, dict] = kwargs.model_data or model_uris.retrieve(
        model_scope=JumpStartScriptScope.INFERENCE,
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
        instance_type=kwargs.instance_type,
    )

    if isinstance(model_data, str) and model_data.startswith("s3://") and model_data.endswith("/"):
        old_model_data_str = model_data
        model_data = {
            "S3DataSource": {
                "S3Uri": model_data,
                "S3DataType": "S3Prefix",
                "CompressionType": "None",
            }
        }
        if kwargs.model_data:
            JUMPSTART_LOGGER.info(
                "S3 prefix model_data detected for JumpStartModel: '%s'. "
                "Converting to S3DataSource dictionary: '%s'.",
                old_model_data_str,
                json.dumps(model_data),
            )

    kwargs.model_data = model_data

    return kwargs


def _add_source_dir_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets source dir based on default or override, returns full kwargs."""

    source_dir = kwargs.source_dir

    if _model_supports_inference_script_uri(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    ):
        source_dir = source_dir or script_uris.retrieve(
            script_scope=JumpStartScriptScope.INFERENCE,
            model_id=kwargs.model_id,
            model_version=kwargs.model_version,
            region=kwargs.region,
            tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
            tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
            sagemaker_session=kwargs.sagemaker_session,
        )

    kwargs.source_dir = source_dir

    return kwargs


def _add_entry_point_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets entry point based on default or override, returns full kwargs."""

    entry_point = kwargs.entry_point

    if _model_supports_inference_script_uri(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    ):

        entry_point = entry_point or INFERENCE_ENTRY_POINT_SCRIPT_NAME

    kwargs.entry_point = entry_point

    return kwargs


def _add_env_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets env based on default or override, returns full kwargs."""

    env = kwargs.env

    if env is None:
        env = {}

    extra_env_vars = environment_variables.retrieve_default(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        include_aws_sdk_env_vars=False,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
        script=JumpStartScriptScope.INFERENCE,
        instance_type=kwargs.instance_type,
    )

    for key, value in extra_env_vars.items():
        update_dict_if_key_not_present(
            env,
            key,
            value,
        )

    if env == {}:
        env = None

    kwargs.env = env

    return kwargs


def _add_model_package_arn_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets model package arn based on default or override, returns full kwargs."""

    model_package_arn = kwargs.model_package_arn or _retrieve_model_package_arn(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        instance_type=kwargs.instance_type,
        scope=JumpStartScriptScope.INFERENCE,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    kwargs.model_package_arn = model_package_arn
    return kwargs


def _add_extra_model_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets extra kwargs based on default or override, returns full kwargs."""

    model_kwargs_to_add = _retrieve_model_init_kwargs(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    for key, value in model_kwargs_to_add.items():
        if getattr(kwargs, key) is None:
            resolved_value = resolve_model_sagemaker_config_field(
                field_name=key,
                field_val=value,
                sagemaker_session=kwargs.sagemaker_session,
            )
            setattr(kwargs, key, resolved_value)

    return kwargs


def _add_predictor_cls_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets predictor class based on default or override, returns full kwargs."""

    predictor_cls = kwargs.predictor_cls or Predictor

    kwargs.predictor_cls = predictor_cls
    return kwargs


def _add_endpoint_name_to_kwargs(
    kwargs: Optional[JumpStartModelDeployKwargs],
) -> JumpStartModelDeployKwargs:
    """Sets resource name based on default or override, returns full kwargs."""

    default_endpoint_name = _retrieve_resource_name_base(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    kwargs.endpoint_name = kwargs.endpoint_name or (
        name_from_base(default_endpoint_name) if default_endpoint_name is not None else None
    )

    return kwargs


def _add_model_name_to_kwargs(
    kwargs: Optional[JumpStartModelInitKwargs],
) -> JumpStartModelInitKwargs:
    """Sets resource name based on default or override, returns full kwargs."""

    default_model_name = _retrieve_resource_name_base(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    kwargs.name = kwargs.name or (
        name_from_base(default_model_name) if default_model_name is not None else None
    )

    return kwargs


def _add_tags_to_kwargs(kwargs: JumpStartModelDeployKwargs) -> Dict[str, Any]:
    """Sets tags based on default or override, returns full kwargs."""

    full_model_version = verify_model_region_and_return_specs(
        model_id=kwargs.model_id,
        version=kwargs.model_version,
        scope=JumpStartScriptScope.INFERENCE,
        region=kwargs.region,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        sagemaker_session=kwargs.sagemaker_session,
    ).version

    if kwargs.sagemaker_session.settings.include_jumpstart_tags:
        kwargs.tags = add_jumpstart_model_id_version_tags(
            kwargs.tags, kwargs.model_id, full_model_version
        )

    return kwargs


def _add_deploy_extra_kwargs(kwargs: JumpStartModelInitKwargs) -> Dict[str, Any]:
    """Sets extra kwargs based on default or override, returns full kwargs."""

    deploy_kwargs_to_add = _retrieve_model_deploy_kwargs(
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        instance_type=kwargs.instance_type,
        region=kwargs.region,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    for key, value in deploy_kwargs_to_add.items():
        if getattr(kwargs, key) is None:
            setattr(kwargs, key, value)

    return kwargs


def _add_resources_to_kwargs(kwargs: JumpStartModelInitKwargs) -> JumpStartModelInitKwargs:
    """Sets the resource requirements based on the default or an override. Returns full kwargs."""

    kwargs.resources = kwargs.resources or resource_requirements.retrieve_default(
        region=kwargs.region,
        model_id=kwargs.model_id,
        model_version=kwargs.model_version,
        scope=JumpStartScriptScope.INFERENCE,
        tolerate_deprecated_model=kwargs.tolerate_deprecated_model,
        tolerate_vulnerable_model=kwargs.tolerate_vulnerable_model,
        sagemaker_session=kwargs.sagemaker_session,
    )

    return kwargs


def get_deploy_kwargs(
    model_id: str,
    model_version: Optional[str] = None,
    region: Optional[str] = None,
    initial_instance_count: Optional[int] = None,
    instance_type: Optional[str] = None,
    serializer: Optional[BaseSerializer] = None,
    deserializer: Optional[BaseDeserializer] = None,
    accelerator_type: Optional[str] = None,
    endpoint_name: Optional[str] = None,
    tags: List[Dict[str, str]] = None,
    kms_key: Optional[str] = None,
    wait: Optional[bool] = None,
    data_capture_config: Optional[DataCaptureConfig] = None,
    async_inference_config: Optional[AsyncInferenceConfig] = None,
    serverless_inference_config: Optional[ServerlessInferenceConfig] = None,
    volume_size: Optional[int] = None,
    model_data_download_timeout: Optional[int] = None,
    container_startup_health_check_timeout: Optional[int] = None,
    inference_recommendation_id: Optional[str] = None,
    explainer_config: Optional[ExplainerConfig] = None,
    tolerate_vulnerable_model: Optional[bool] = None,
    tolerate_deprecated_model: Optional[bool] = None,
    sagemaker_session: Optional[Session] = None,
    accept_eula: Optional[bool] = None,
    endpoint_logging: Optional[bool] = None,
    resources: Optional[ResourceRequirements] = None,
    managed_instance_scaling: Optional[str] = None,
    endpoint_type: Optional[EndpointType] = None,
) -> JumpStartModelDeployKwargs:
    """Returns kwargs required to call `deploy` on `sagemaker.estimator.Model` object."""

    deploy_kwargs: JumpStartModelDeployKwargs = JumpStartModelDeployKwargs(
        model_id=model_id,
        model_version=model_version,
        region=region,
        initial_instance_count=initial_instance_count,
        instance_type=instance_type,
        serializer=serializer,
        deserializer=deserializer,
        accelerator_type=accelerator_type,
        endpoint_name=endpoint_name,
        tags=tags,
        kms_key=kms_key,
        wait=wait,
        data_capture_config=data_capture_config,
        async_inference_config=async_inference_config,
        serverless_inference_config=serverless_inference_config,
        volume_size=volume_size,
        model_data_download_timeout=model_data_download_timeout,
        container_startup_health_check_timeout=container_startup_health_check_timeout,
        inference_recommendation_id=inference_recommendation_id,
        explainer_config=explainer_config,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        sagemaker_session=sagemaker_session,
        accept_eula=accept_eula,
        endpoint_logging=endpoint_logging,
        resources=resources,
    )

    deploy_kwargs = _add_sagemaker_session_to_kwargs(kwargs=deploy_kwargs)

    deploy_kwargs = _add_model_version_to_kwargs(kwargs=deploy_kwargs)

    deploy_kwargs = _add_endpoint_name_to_kwargs(kwargs=deploy_kwargs)

    deploy_kwargs = _add_instance_type_to_kwargs(kwargs=deploy_kwargs)

    deploy_kwargs.initial_instance_count = initial_instance_count or 1

    deploy_kwargs = _add_deploy_extra_kwargs(kwargs=deploy_kwargs)

    deploy_kwargs = _add_tags_to_kwargs(kwargs=deploy_kwargs)

    if endpoint_type == EndpointType.INFERENCE_COMPONENT_BASED:
        deploy_kwargs = _add_resources_to_kwargs(kwargs=deploy_kwargs)
        deploy_kwargs.endpoint_type = endpoint_type
        deploy_kwargs.managed_instance_scaling = managed_instance_scaling

    return deploy_kwargs


def get_register_kwargs(
    model_id: str,
    model_version: Optional[str] = None,
    region: Optional[str] = None,
    tolerate_deprecated_model: Optional[bool] = None,
    tolerate_vulnerable_model: Optional[bool] = None,
    sagemaker_session: Optional[Any] = None,
    supported_content_types: List[str] = None,
    response_types: List[str] = None,
    inference_instances: Optional[List[str]] = None,
    transform_instances: Optional[List[str]] = None,
    model_package_group_name: Optional[str] = None,
    image_uri: Optional[str] = None,
    model_metrics: Optional[ModelMetrics] = None,
    metadata_properties: Optional[MetadataProperties] = None,
    approval_status: Optional[str] = None,
    description: Optional[str] = None,
    drift_check_baselines: Optional[DriftCheckBaselines] = None,
    customer_metadata_properties: Optional[Dict[str, str]] = None,
    validation_specification: Optional[str] = None,
    domain: Optional[str] = None,
    task: Optional[str] = None,
    sample_payload_url: Optional[str] = None,
    framework: Optional[str] = None,
    framework_version: Optional[str] = None,
    nearest_model_name: Optional[str] = None,
    data_input_configuration: Optional[str] = None,
    skip_model_validation: Optional[str] = None,
) -> JumpStartModelRegisterKwargs:
    """Returns kwargs required to call `register` on `sagemaker.estimator.Model` object."""

    register_kwargs = JumpStartModelRegisterKwargs(
        model_id=model_id,
        model_version=model_version,
        region=region,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        sagemaker_session=sagemaker_session,
        content_types=supported_content_types,
        response_types=response_types,
        inference_instances=inference_instances,
        transform_instances=transform_instances,
        model_package_group_name=model_package_group_name,
        image_uri=image_uri,
        model_metrics=model_metrics,
        metadata_properties=metadata_properties,
        approval_status=approval_status,
        description=description,
        drift_check_baselines=drift_check_baselines,
        customer_metadata_properties=customer_metadata_properties,
        validation_specification=validation_specification,
        domain=domain,
        task=task,
        sample_payload_url=sample_payload_url,
        framework=framework,
        framework_version=framework_version,
        nearest_model_name=nearest_model_name,
        data_input_configuration=data_input_configuration,
        skip_model_validation=skip_model_validation,
    )

    model_specs = verify_model_region_and_return_specs(
        model_id=model_id,
        version=model_version,
        region=region,
        scope=JumpStartScriptScope.INFERENCE,
        sagemaker_session=sagemaker_session,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
    )

    register_kwargs.content_types = (
        register_kwargs.content_types or model_specs.predictor_specs.supported_content_types
    )
    register_kwargs.response_types = (
        register_kwargs.response_types or model_specs.predictor_specs.supported_accept_types
    )

    return register_kwargs


def get_init_kwargs(
    model_id: str,
    model_from_estimator: bool = False,
    model_version: Optional[str] = None,
    tolerate_vulnerable_model: Optional[bool] = None,
    tolerate_deprecated_model: Optional[bool] = None,
    instance_type: Optional[str] = None,
    region: Optional[str] = None,
    image_uri: Optional[Union[str, PipelineVariable]] = None,
    model_data: Optional[Union[str, PipelineVariable, dict]] = None,
    role: Optional[str] = None,
    predictor_cls: Optional[callable] = None,
    env: Optional[Dict[str, Union[str, PipelineVariable]]] = None,
    name: Optional[str] = None,
    vpc_config: Optional[Dict[str, List[Union[str, PipelineVariable]]]] = None,
    sagemaker_session: Optional[Session] = None,
    enable_network_isolation: Union[bool, PipelineVariable] = None,
    model_kms_key: Optional[str] = None,
    image_config: Optional[Dict[str, Union[str, PipelineVariable]]] = None,
    source_dir: Optional[str] = None,
    code_location: Optional[str] = None,
    entry_point: Optional[str] = None,
    container_log_level: Optional[Union[int, PipelineVariable]] = None,
    dependencies: Optional[List[str]] = None,
    git_config: Optional[Dict[str, str]] = None,
    model_package_arn: Optional[str] = None,
    training_instance_type: Optional[str] = None,
    disable_instance_type_logging: bool = False,
    resources: Optional[ResourceRequirements] = None,
) -> JumpStartModelInitKwargs:
    """Returns kwargs required to instantiate `sagemaker.estimator.Model` object."""

    model_init_kwargs: JumpStartModelInitKwargs = JumpStartModelInitKwargs(
        model_id=model_id,
        model_version=model_version,
        instance_type=instance_type,
        region=region,
        image_uri=image_uri,
        model_data=model_data,
        source_dir=source_dir,
        entry_point=entry_point,
        env=env,
        predictor_cls=predictor_cls,
        role=role,
        name=name,
        vpc_config=vpc_config,
        sagemaker_session=sagemaker_session,
        enable_network_isolation=enable_network_isolation,
        model_kms_key=model_kms_key,
        image_config=image_config,
        code_location=code_location,
        container_log_level=container_log_level,
        dependencies=dependencies,
        git_config=git_config,
        tolerate_deprecated_model=tolerate_deprecated_model,
        tolerate_vulnerable_model=tolerate_vulnerable_model,
        model_package_arn=model_package_arn,
        training_instance_type=training_instance_type,
        resources=resources,
    )

    model_init_kwargs = _add_model_version_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_vulnerable_and_deprecated_status_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_region_to_kwargs(kwargs=model_init_kwargs)
    model_init_kwargs = _add_sagemaker_session_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_model_name_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_instance_type_to_kwargs(
        kwargs=model_init_kwargs, disable_instance_type_logging=disable_instance_type_logging
    )

    model_init_kwargs = _add_image_uri_to_kwargs(kwargs=model_init_kwargs)

    # we use the model artifact from the training job output
    if not model_from_estimator:
        model_init_kwargs = _add_model_data_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_source_dir_to_kwargs(kwargs=model_init_kwargs)
    model_init_kwargs = _add_entry_point_to_kwargs(kwargs=model_init_kwargs)
    model_init_kwargs = _add_env_to_kwargs(kwargs=model_init_kwargs)
    model_init_kwargs = _add_predictor_cls_to_kwargs(kwargs=model_init_kwargs)
    model_init_kwargs = _add_extra_model_kwargs(kwargs=model_init_kwargs)
    model_init_kwargs = _add_role_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_model_package_arn_to_kwargs(kwargs=model_init_kwargs)

    model_init_kwargs = _add_resources_to_kwargs(kwargs=model_init_kwargs)

    return model_init_kwargs
