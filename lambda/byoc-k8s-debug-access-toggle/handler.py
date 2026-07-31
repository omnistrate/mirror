import base64
import hashlib
import io
import json
import os
import ssl
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.signers import RequestSigner


DEFAULT_NAMESPACE = "dataplane-agent"
DEFAULT_DEBUG_ACCESS_POLICY_NAME = "block-k8s-api-proxy"
DEFAULT_DP_AGENT_SELECTOR = "app.kubernetes.io/instance=dp-agent"
DEFAULT_CLUSTER_TAG_KEY = "omnistrate.com/private-link"
DEFAULT_CLUSTER_TAG_VALUE = "true"
DEFAULT_HOST_CLUSTER_TAG_KEY = "omnistrate.com/host-cluster-id"
WORKER_FUNCTION_PREFIX = "byoc-k8s-debug-access"
LAMBDA_CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=120, retries={"max_attempts": 2})

_WORKER_ZIP_BYTES = None


def _generate_eks_token(cluster_name: str, region: str) -> str:
    session = boto3.Session()
    sts_client = session.client("sts", region_name=region)
    signer = RequestSigner(
        sts_client.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.events,
    )

    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": "",
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }

    signed_url = signer.generate_presigned_url(
        params, region_name=region, expires_in=60, operation_name="GetCallerIdentity"
    )

    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode()).decode().rstrip("=")


def _current_execution_role_arn(region: str) -> str:
    arn = boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        return arn

    prefix, assumed_role = arn.split(":assumed-role/", 1)
    role_name = assumed_role.split("/", 1)[0]
    arn_parts = prefix.split(":")
    partition = arn_parts[1]
    account_id = arn_parts[4]
    return f"arn:{partition}:iam::{account_id}:role/{role_name}"


def _verify_cluster_admin_access_entry(cluster_name: str, region: str, principal_arn: str):
    eks = boto3.client("eks", region_name=region)

    try:
        eks.describe_access_entry(clusterName=cluster_name, principalArn=principal_arn)
    except eks.exceptions.ResourceNotFoundException:
        raise RuntimeError(
            "bootstrap-owned EKS access entry is not ready for "
            f"{principal_arn} on cluster {cluster_name}"
        )


def _normalize_properties(event):
    props = dict(event.get("ResourceProperties") or {})
    for key, value in event.items():
        if key not in {
            "RequestType",
            "ResponseURL",
            "StackId",
            "RequestId",
            "LogicalResourceId",
            "ResourceType",
            "ResourceProperties",
            "OldResourceProperties",
        }:
            props.setdefault(key, value)
    return props


def _prop(props, *names, default=""):
    for name in names:
        if name in props and props[name] is not None:
            return props[name]
    return default


def _split_csv(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _parse_port(value) -> int:
    if value is None or value == "":
        return 0
    port = int(str(value))
    if port < 1 or port > 65535:
        raise ValueError("managerK8sPort must be between 1 and 65535")
    return port


def _parse_bool(value, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{field_name} must be true or false")


def _parse_port_from_manager_address(address: str) -> int:
    port = address.rsplit(":", 1)[-1]
    if not port.isdigit():
        raise ValueError(f"failed to parse Kubernetes debug access port from MANAGER_CONNECT_K8S_ADDRESS={address}")
    return _parse_port(port)


def _write_ca_file(ca_data: str) -> str:
    fd, path = tempfile.mkstemp(prefix="eks-ca-", suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(ca_data))
    return path


def _k8s_request(endpoint: str, ca_data: str, token: str, method: str, path: str, body=None, content_type: str = "application/json"):
    ca_path = _write_ca_file(ca_data)
    try:
        url = endpoint.rstrip("/") + path
        data = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type

        req = Request(url, data=data, headers=headers, method=method)
        context = ssl.create_default_context(cafile=ca_path)
        with urlopen(req, timeout=30, context=context) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    finally:
        try:
            os.unlink(ca_path)
        except FileNotFoundError:
            pass


def _k8s_get_or_none(endpoint: str, ca_data: str, token: str, path: str):
    try:
        return _k8s_request(endpoint, ca_data, token, "GET", path)
    except HTTPError as e:
        if e.code == 404:
            return None
        raise


def _discover_manager_k8s_port(endpoint: str, ca_data: str, token: str, namespace: str) -> int:
    selector = quote(DEFAULT_DP_AGENT_SELECTOR, safe="")
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments?labelSelector={selector}"
    deployments = _k8s_request(endpoint, ca_data, token, "GET", path)
    items = (deployments or {}).get("items") or []
    if not items:
        raise RuntimeError(f"failed to find dp-agent deployment in namespace {namespace}")

    containers = items[0].get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    for container in containers:
        for env in container.get("env") or []:
            if env.get("name") == "MANAGER_CONNECT_K8S_ADDRESS" and env.get("value"):
                return _parse_port_from_manager_address(env["value"])

    raise RuntimeError("failed to find MANAGER_CONNECT_K8S_ADDRESS on the dp-agent deployment")


def _restart_dp_agent_deployments(endpoint: str, ca_data: str, token: str, namespace: str):
    selector = quote(DEFAULT_DP_AGENT_SELECTOR, safe="")
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments?labelSelector={selector}"
    deployments = _k8s_request(endpoint, ca_data, token, "GET", path)
    items = (deployments or {}).get("items") or []
    if not items:
        raise RuntimeError(f"failed to find dp-agent deployment in namespace {namespace}")

    restarted = []
    restarted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "k8s-debug-access-toggle/restarted-at": restarted_at,
                    },
                },
            },
        },
    }
    for deployment in items:
        name = deployment.get("metadata", {}).get("name")
        if not name:
            continue
        deployment_path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        updated = _k8s_request(
            endpoint,
            ca_data,
            token,
            "PATCH",
            deployment_path,
            patch,
            content_type="application/strategic-merge-patch+json",
        )
        restarted.append(name)
        try:
            _wait_deployment_rollout(endpoint, ca_data, token, namespace, name, updated)
        except TimeoutError as e:
            print(json.dumps({
                "level": "warning",
                "message": "dp-agent restart requested but rollout did not complete before Lambda response",
                "deployment": f"{namespace}/{name}",
                "error": str(e),
            }))
    return restarted


def _wait_deployment_rollout(endpoint: str, ca_data: str, token: str, namespace: str, name: str, deployment):
    target_generation = (deployment or {}).get("metadata", {}).get("generation", 0)
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
    for _ in range(10):
        current = _k8s_request(endpoint, ca_data, token, "GET", path)
        spec_replicas = current.get("spec", {}).get("replicas", 1)
        status = current.get("status", {})
        if (
            status.get("observedGeneration", 0) >= target_generation
            and status.get("updatedReplicas", 0) == spec_replicas
            and status.get("readyReplicas", 0) == spec_replicas
            and status.get("availableReplicas", 0) == spec_replicas
        ):
            return
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for dp-agent deployment {namespace}/{name} rollout")


def _render_debug_access_policy(namespace: str, policy_name: str, manager_k8s_port: int):
    egress = []
    lower_end = manager_k8s_port - 1
    upper_start = manager_k8s_port + 1
    if lower_end >= 1:
        egress.append({
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
            "ports": [{"protocol": "TCP", "port": 1, "endPort": lower_end}],
        })
    if upper_start <= 65535:
        egress.append({
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
            "ports": [{"protocol": "TCP", "port": upper_start, "endPort": 65535}],
        })
    egress.append({
        "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
        "ports": [{"protocol": "UDP", "port": 1, "endPort": 65535}],
    })

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/component": "k8s-debug-access-toggle",
            },
        },
        "spec": {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/instance": "dp-agent",
                },
            },
            "policyTypes": ["Egress"],
            "egress": egress,
        },
    }


def _apply_debug_access_policy(endpoint: str, ca_data: str, token: str, policy):
    namespace = policy["metadata"]["namespace"]
    policy_name = policy["metadata"]["name"]
    policy_path = f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/{policy_name}"
    existing = _k8s_get_or_none(endpoint, ca_data, token, policy_path)
    if existing:
        policy["metadata"]["resourceVersion"] = existing.get("metadata", {}).get("resourceVersion")
        _k8s_request(endpoint, ca_data, token, "PUT", policy_path, policy)
        return "updated"

    collection_path = f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies"
    _k8s_request(endpoint, ca_data, token, "POST", collection_path, policy)
    return "created"


def _delete_debug_access_policy(endpoint: str, ca_data: str, token: str, namespace: str, policy_name: str):
    policy_path = f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/{policy_name}"
    try:
        _k8s_request(endpoint, ca_data, token, "DELETE", policy_path, {"apiVersion": "v1", "kind": "DeleteOptions"})
        return "deleted"
    except HTTPError as e:
        if e.code == 404:
            return "not_found"
        raise


def _send_cfn_response(event, context, status: str, data: dict, reason: str = ""):
    response_url = event.get("ResponseURL")
    if not response_url:
        return

    physical_id = event.get("PhysicalResourceId") or event.get("LogicalResourceId") or getattr(
        context, "log_stream_name", "k8s-debug-access-toggle"
    )
    response_body = {
        "Status": status,
        "Reason": reason[:512] if reason else f"See CloudWatch Logs for request {getattr(context, 'aws_request_id', 'unknown')}",
        "PhysicalResourceId": physical_id,
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "NoEcho": False,
        "Data": data or {},
    }
    encoded = json.dumps(response_body).encode("utf-8")
    req = Request(
        response_url,
        data=encoded,
        headers={"content-type": "", "content-length": str(len(encoded))},
        method="PUT",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        print(f"failed to send CloudFormation response: {e}")


def _cfn_summary(result):
    return {
        "debugAccessEnabled": result.get("debugAccessEnabled"),
        "discoveredClusterCount": result.get("discoveredClusterCount", 0),
        "reconciledClusterCount": result.get("reconciledClusterCount", 0),
    }


def _queue_controller_from_cfn(event, context):
    async_event = dict(event)
    async_event.pop("ResponseURL", None)
    async_event["Source"] = "CloudFormation"

    try:
        boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"), config=LAMBDA_CLIENT_CONFIG).invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType="Event",
            Payload=json.dumps(async_event).encode("utf-8"),
        )
    except Exception as e:
        print(json.dumps({
            "level": "warning",
            "message": "failed to queue Kubernetes debug access reconciliation from CloudFormation",
            "error": str(e),
        }))
        return {
            "success": True,
            "queued": False,
        }

    return {
        "success": True,
        "queued": True,
    }


def _handle_worker_target(props):
    cluster_name = _prop(props, "clusterName", "ClusterName")
    region = _prop(props, "clusterRegion", "ClusterRegion", "region", "Region")
    endpoint = _prop(props, "endpoint", "Endpoint")
    ca_data = _prop(props, "certificateAuthorityData", "CertificateAuthorityData")
    namespace = _prop(props, "namespace", "Namespace", default=DEFAULT_NAMESPACE)
    policy_name = _prop(props, "policyName", "PolicyName", default=DEFAULT_DEBUG_ACCESS_POLICY_NAME)
    debug_access_enabled = _parse_bool(_prop(props, "debugAccessEnabled", "DebugAccessEnabled", default="true"), "debugAccessEnabled")

    if not cluster_name:
        raise ValueError("clusterName is required")
    if not region:
        raise ValueError("clusterRegion is required")
    if not endpoint:
        raise ValueError("endpoint is required")
    if not ca_data:
        raise ValueError("certificateAuthorityData is required")

    token = _generate_eks_token(cluster_name, region)

    if debug_access_enabled:
        action = _delete_debug_access_policy(endpoint, ca_data, token, namespace, policy_name)
        restarted_deployments = _restart_dp_agent_deployments(endpoint, ca_data, token, namespace)
        return {
            "success": True,
            "debugAccessEnabled": True,
            "action": action,
            "cluster": cluster_name,
            "region": region,
            "restartedDeployments": restarted_deployments,
        }

    manager_k8s_port = _parse_port(_prop(props, "managerK8sPort", "ManagerK8sPort", default=""))
    if manager_k8s_port == 0:
        manager_k8s_port = _discover_manager_k8s_port(endpoint, ca_data, token, namespace)

    policy = _render_debug_access_policy(namespace, policy_name, manager_k8s_port)
    action = _apply_debug_access_policy(endpoint, ca_data, token, policy)
    restarted_deployments = _restart_dp_agent_deployments(endpoint, ca_data, token, namespace)
    return {
        "success": True,
        "debugAccessEnabled": False,
        "action": action,
        "cluster": cluster_name,
        "region": region,
        "managerK8sPort": manager_k8s_port,
        "restartedDeployments": restarted_deployments,
    }


def _configured_region_targets(props):
    configured = _split_csv(_prop(props, "regions", "Regions", "K8sDebugAccessRegions", default=os.environ.get("K8S_DEBUG_ACCESS_REGIONS", "")))
    if configured:
        regions = []
        host_cluster_ids_by_region = {}
        all_hosts_regions = set()
        for target in configured:
            region, separator, host_cluster_id = target.partition(":")
            region = region.strip()
            host_cluster_id = host_cluster_id.strip()
            if not region:
                raise ValueError(f"invalid region target {target}")
            if separator and not host_cluster_id:
                raise ValueError(f"invalid region target {target}; expected region or region:host-cluster-id")
            if region not in regions:
                regions.append(region)
            if not separator:
                all_hosts_regions.add(region)
                host_cluster_ids_by_region.pop(region, None)
                continue
            if region not in all_hosts_regions:
                host_cluster_ids_by_region.setdefault(region, set()).add(host_cluster_id)
        return regions, host_cluster_ids_by_region

    ec2 = boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    regions = ec2.describe_regions(AllRegions=False)["Regions"]
    return sorted(region["RegionName"] for region in regions if region.get("RegionName")), {}


def _cluster_tags(eks, cluster):
    tags = cluster.get("tags")
    if tags is not None:
        return tags
    cluster_arn = cluster.get("arn")
    if not cluster_arn:
        return {}
    return eks.list_tags_for_resource(resourceArn=cluster_arn).get("tags") or {}


def _dedupe(values, limit):
    result = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _worker_vpc_config(cluster):
    vpc_config = cluster.get("resourcesVpcConfig") or {}
    subnet_ids = _dedupe(sorted(vpc_config.get("subnetIds") or []), 16)
    security_group_ids = []
    if vpc_config.get("clusterSecurityGroupId"):
        security_group_ids.append(vpc_config["clusterSecurityGroupId"])
    security_group_ids = _dedupe(security_group_ids, 5)

    if not subnet_ids:
        raise ValueError("cluster has no subnet IDs in resourcesVpcConfig")
    if not security_group_ids:
        raise ValueError("cluster has no security group IDs in resourcesVpcConfig")
    return subnet_ids, security_group_ids


def _discover_private_clusters(props):
    tag_key = _prop(
        props,
        "clusterTagKey",
        "ClusterTagKey",
        "K8sDebugAccessClusterTagKey",
        default=os.environ.get("K8S_DEBUG_ACCESS_CLUSTER_TAG_KEY", DEFAULT_CLUSTER_TAG_KEY),
    )
    tag_value = _prop(
        props,
        "clusterTagValue",
        "ClusterTagValue",
        "K8sDebugAccessClusterTagValue",
        default=os.environ.get("K8S_DEBUG_ACCESS_CLUSTER_TAG_VALUE", DEFAULT_CLUSTER_TAG_VALUE),
    )
    host_cluster_tag_key = _prop(
        props,
        "hostClusterTagKey",
        "HostClusterTagKey",
        "K8sDebugAccessHostClusterTagKey",
        default=os.environ.get("K8S_DEBUG_ACCESS_HOST_CLUSTER_TAG_KEY", DEFAULT_HOST_CLUSTER_TAG_KEY),
    )
    if not tag_key or not tag_value:
        raise ValueError("cluster tag key and value are required")

    targets = []
    regions, host_cluster_ids_by_region = _configured_region_targets(props)
    for region in regions:
        eks = boto3.client("eks", region_name=region)
        paginator = eks.get_paginator("list_clusters")
        for page in paginator.paginate():
            for cluster_name in page.get("clusters") or []:
                try:
                    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
                except eks.exceptions.ResourceNotFoundException:
                    continue

                tags = _cluster_tags(eks, cluster)
                if tags.get(tag_key) != tag_value:
                    continue

                vpc_config = cluster.get("resourcesVpcConfig") or {}
                if not vpc_config.get("endpointPrivateAccess"):
                    continue

                host_cluster_id = tags.get(host_cluster_tag_key) or cluster["name"]
                region_host_ids = host_cluster_ids_by_region.get(region)
                if region_host_ids is not None and host_cluster_id not in region_host_ids:
                    continue

                subnet_ids, security_group_ids = _worker_vpc_config(cluster)
                targets.append({
                    "clusterName": cluster["name"],
                    "clusterRegion": region,
                    "clusterArn": cluster.get("arn", ""),
                    "endpoint": cluster["endpoint"],
                    "certificateAuthorityData": (cluster.get("certificateAuthority") or {}).get("data", ""),
                    "subnetIds": subnet_ids,
                    "securityGroupIds": security_group_ids,
                    "hostClusterId": host_cluster_id,
                    "vpcId": vpc_config.get("vpcId", ""),
                    "tags": tags,
                })
    return targets


def _worker_zip_bytes():
    global _WORKER_ZIP_BYTES
    if _WORKER_ZIP_BYTES is not None:
        return _WORKER_ZIP_BYTES

    task_root = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")
    handler_path = ""
    for candidate in ("handler.py", "index.py"):
        path = os.path.join(task_root, candidate)
        if os.path.exists(path):
            handler_path = path
            break
    if not handler_path:
        raise FileNotFoundError("failed to find Lambda handler source")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(handler_path, "handler.py")
    _WORKER_ZIP_BYTES = buffer.getvalue()
    return _WORKER_ZIP_BYTES


def _wait_worker_active(lambda_client, function_name: str):
    poll_seconds = 2
    wait_seconds = int(os.environ.get("K8S_DEBUG_ACCESS_WORKER_ACTIVE_WAIT_SECONDS", "300") or "300")
    attempts = max(1, wait_seconds // poll_seconds)
    last_state = ""
    last_update_status = ""
    last_state_reason = ""
    last_update_status_reason = ""
    print(json.dumps({
        "message": "waiting for worker Lambda to become active",
        "functionName": function_name,
        "waitSeconds": wait_seconds,
    }))
    for attempt in range(attempts):
        try:
            cfg = lambda_client.get_function_configuration(FunctionName=function_name)
        except lambda_client.exceptions.ResourceNotFoundException:
            if attempt % 15 == 0:
                print(json.dumps({
                    "message": "worker Lambda is not visible yet",
                    "functionName": function_name,
                    "elapsedSeconds": attempt * poll_seconds,
                }))
            time.sleep(poll_seconds)
            continue

        state = cfg.get("State")
        update_status = cfg.get("LastUpdateStatus")
        state_reason = cfg.get("StateReason") or ""
        update_status_reason = cfg.get("LastUpdateStatusReason") or ""
        if (
            state != last_state
            or update_status != last_update_status
            or state_reason != last_state_reason
            or update_status_reason != last_update_status_reason
            or attempt % 15 == 0
        ):
            print(json.dumps({
                "message": "worker Lambda activation status",
                "functionName": function_name,
                "elapsedSeconds": attempt * poll_seconds,
                "state": state,
                "lastUpdateStatus": update_status,
                "stateReason": state_reason,
                "lastUpdateStatusReason": update_status_reason,
            }))
        last_state = state or ""
        last_update_status = update_status or ""
        last_state_reason = state_reason
        last_update_status_reason = update_status_reason
        if state == "Failed" or update_status == "Failed":
            raise RuntimeError(f"worker Lambda {function_name} failed to become active: {cfg.get('StateReason') or cfg.get('LastUpdateStatusReason')}")
        if state == "Active" and update_status in ("Successful", None):
            return
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"timed out after {wait_seconds}s waiting for worker Lambda {function_name} to become active "
        f"(state={last_state or 'unknown'}, lastUpdateStatus={last_update_status or 'unknown'}, "
        f"stateReason={last_state_reason or 'unknown'}, lastUpdateStatusReason={last_update_status_reason or 'unknown'})"
    )


def _wait_worker_deleted(lambda_client, function_name: str):
    for _ in range(30):
        try:
            lambda_client.get_function_configuration(FunctionName=function_name)
            time.sleep(2)
        except lambda_client.exceptions.ResourceNotFoundException:
            return
    print(json.dumps({
        "level": "warning",
        "message": "timed out waiting for worker Lambda deletion",
        "functionName": function_name,
    }))


def _lambda_call_with_conflict_retry(lambda_client, function_name: str, operation_name: str, **kwargs):
    operation = getattr(lambda_client, operation_name)
    for attempt in range(10):
        try:
            return operation(FunctionName=function_name, **kwargs)
        except lambda_client.exceptions.ResourceConflictException:
            if attempt == 9:
                raise
            print(json.dumps({
                "level": "warning",
                "message": "retrying worker Lambda operation after conflict",
                "functionName": function_name,
                "action": operation_name,
                "attempt": attempt + 1,
            }))
            _wait_worker_active(lambda_client, function_name)
            time.sleep(2)


def _worker_function_name(target):
    digest = hashlib.sha256(f"{target['clusterRegion']}:{target['clusterName']}:{target.get('vpcId', '')}".encode("utf-8")).hexdigest()
    return f"{WORKER_FUNCTION_PREFIX}-{digest[:24]}"


def _tag_worker_function(lambda_client, function_name: str, tags: dict):
    try:
        fn = lambda_client.get_function(FunctionName=function_name)
        function_arn = fn.get("Configuration", {}).get("FunctionArn")
        if function_arn:
            lambda_client.tag_resource(Resource=function_arn, Tags=tags)
    except ClientError as e:
        print(json.dumps({"message": "failed to tag worker Lambda", "functionName": function_name, "error": str(e)}))


def _ensure_worker_function(lambda_client, function_name: str, role_arn: str, target: dict, tags: dict):
    vpc_config = {
        "SubnetIds": target["subnetIds"],
        "SecurityGroupIds": target["securityGroupIds"],
    }
    try:
        lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.worker_handler",
            Code={"ZipFile": _worker_zip_bytes()},
            Timeout=300,
            MemorySize=256,
            PackageType="Zip",
            Architectures=["x86_64"],
            VpcConfig=vpc_config,
            Tags=tags,
        )
        _wait_worker_active(lambda_client, function_name)
        return
    except lambda_client.exceptions.ResourceConflictException:
        _wait_worker_active(lambda_client, function_name)

    _lambda_call_with_conflict_retry(
        lambda_client,
        function_name,
        "update_function_code",
        ZipFile=_worker_zip_bytes(),
    )
    _wait_worker_active(lambda_client, function_name)
    _lambda_call_with_conflict_retry(
        lambda_client,
        function_name,
        "update_function_configuration",
        Runtime="python3.12",
        Role=role_arn,
        Handler="handler.worker_handler",
        Timeout=300,
        MemorySize=256,
        VpcConfig=vpc_config,
    )
    _wait_worker_active(lambda_client, function_name)
    _tag_worker_function(lambda_client, function_name, tags)


def _delete_worker_function(target):
    region = target["clusterRegion"]
    lambda_client = boto3.client("lambda", region_name=region, config=LAMBDA_CLIENT_CONFIG)
    function_name = _worker_function_name(target)
    try:
        try:
            cfg = lambda_client.get_function_configuration(FunctionName=function_name)
        except lambda_client.exceptions.ResourceNotFoundException:
            return

        if cfg.get("State") == "Active":
            _lambda_call_with_conflict_retry(
                lambda_client,
                function_name,
                "update_function_configuration",
                VpcConfig={"SubnetIds": [], "SecurityGroupIds": []},
            )
            _wait_worker_active(lambda_client, function_name)
        lambda_client.delete_function(FunctionName=function_name)
        _wait_worker_deleted(lambda_client, function_name)
    except lambda_client.exceptions.ResourceNotFoundException:
        return
    except TimeoutError as e:
        print(json.dumps({"message": "timed out preparing worker Lambda for deletion; deleting directly", "functionName": function_name, "error": str(e)}))
        try:
            lambda_client.delete_function(FunctionName=function_name)
            _wait_worker_deleted(lambda_client, function_name)
        except lambda_client.exceptions.ResourceNotFoundException:
            return
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceConflictException":
            print(json.dumps({"message": "worker Lambda busy during VPC detach; deleting directly", "functionName": function_name, "error": str(e)}))
            lambda_client.delete_function(FunctionName=function_name)
            _wait_worker_deleted(lambda_client, function_name)
        else:
            print(json.dumps({"message": "failed to detach worker Lambda VPC config", "functionName": function_name, "error": str(e)}))


def _invoke_worker(target, props, context, debug_access_enabled: bool, namespace: str, policy_name: str):
    region = target["clusterRegion"]
    lambda_client = boto3.client("lambda", region_name=region, config=LAMBDA_CLIENT_CONFIG)
    function_name = _worker_function_name(target)
    role_arn = _prop(props, "lambdaExecutionRoleArn", "LambdaExecutionRoleArn", default=os.environ.get("LAMBDA_EXECUTION_ROLE_ARN", ""))
    if not role_arn:
        role_arn = _current_execution_role_arn(region)

    tags = {
        "k8s-debug-access-worker": "true",
    }
    tag_key = _prop(
        props,
        "clusterTagKey",
        "ClusterTagKey",
        "K8sDebugAccessClusterTagKey",
        default=os.environ.get("K8S_DEBUG_ACCESS_CLUSTER_TAG_KEY", DEFAULT_CLUSTER_TAG_KEY),
    )
    tag_value = _prop(
        props,
        "clusterTagValue",
        "ClusterTagValue",
        "K8sDebugAccessClusterTagValue",
        default=os.environ.get("K8S_DEBUG_ACCESS_CLUSTER_TAG_VALUE", DEFAULT_CLUSTER_TAG_VALUE),
    )
    if tag_key and tag_value:
        tags[tag_key] = tag_value

    payload = {
        "clusterName": target["clusterName"],
        "clusterRegion": region,
        "endpoint": target["endpoint"],
        "certificateAuthorityData": target["certificateAuthorityData"],
        "debugAccessEnabled": debug_access_enabled,
        "namespace": namespace,
        "policyName": policy_name,
    }
    invocation_type = _prop(
        props,
        "workerInvocationType",
        "WorkerInvocationType",
        default=os.environ.get("K8S_DEBUG_ACCESS_WORKER_INVOCATION_TYPE", "RequestResponse"),
    )
    delete_worker_after_invoke = _parse_bool(
        _prop(
            props,
            "deleteWorkerAfterInvoke",
            "DeleteWorkerAfterInvoke",
            default=os.environ.get("K8S_DEBUG_ACCESS_DELETE_WORKER_AFTER_INVOKE", "false"),
        ),
        "deleteWorkerAfterInvoke",
    )

    try:
        _ensure_worker_function(lambda_client, function_name, role_arn, target, tags)

        if invocation_type == "Event":
            response = lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=json.dumps(payload).encode("utf-8"),
            )
            return {
                "success": True,
                "queued": True,
                "cluster": target["clusterName"],
                "region": region,
                "debugAccessEnabled": debug_access_enabled,
                "statusCode": response.get("StatusCode"),
            }

        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        raw_payload = response["Payload"].read()
        if response.get("FunctionError"):
            raise RuntimeError(raw_payload.decode("utf-8", errors="replace"))
        return json.loads(raw_payload.decode("utf-8")) if raw_payload else {}
    finally:
        if delete_worker_after_invoke and invocation_type != "Event":
            _delete_worker_function(target)


def _handle_controller(event, context):
    props = _normalize_properties(event)

    debug_access_enabled = _parse_bool(
        _prop(
            props,
            "debugAccessEnabled",
            "DebugAccessEnabled",
            "k8sDebugAccessEnabled",
            "K8sDebugAccessEnabled",
            default=os.environ.get("K8S_DEBUG_ACCESS_ENABLED", "true"),
        ),
        "debugAccessEnabled",
    )
    namespace = _prop(props, "namespace", "Namespace", default=os.environ.get("K8S_DEBUG_ACCESS_NAMESPACE", DEFAULT_NAMESPACE))
    policy_name = _prop(props, "policyName", "PolicyName", default=os.environ.get("K8S_DEBUG_ACCESS_POLICY_NAME", DEFAULT_DEBUG_ACCESS_POLICY_NAME))

    delete_request = event.get("RequestType") == "Delete"
    if delete_request:
        debug_access_enabled = True

    targets = _discover_private_clusters(props)
    access_failures = _verify_cluster_admin_access(targets, props)

    results = []
    failures = list(access_failures)
    role_arn = _lambda_execution_role_arn(props)

    def reconcile_target(target):
        if any(
            failure.get("cluster") == target.get("clusterName")
            and failure.get("region") == target.get("clusterRegion")
            for failure in access_failures
        ):
            return None, None

        try:
            result = _invoke_worker(target, props, context, debug_access_enabled, namespace, policy_name)
            return result, None
        except Exception as e:
            if _is_k8s_auth_error(e):
                print(json.dumps({
                    "message": "Kubernetes auth failed; bootstrap-owned EKS access entry may not be effective yet",
                    "cluster": target.get("clusterName"),
                    "region": target.get("clusterRegion"),
                    "roleArn": role_arn,
                    "error": str(e),
                }))
            failure = {
                "cluster": target.get("clusterName"),
                "region": target.get("clusterRegion"),
                "error": str(e),
            }
            print(json.dumps({"message": "failed to reconcile cluster", **failure}))
            return None, failure

    max_workers = min(len(targets), int(os.environ.get("K8S_DEBUG_ACCESS_MAX_WORKERS", "4") or "4"))
    if max_workers < 1:
        max_workers = 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(reconcile_target, target) for target in targets]
        for future in as_completed(futures):
            result, failure = future.result()
            if result is not None:
                results.append(result)
            if failure is not None:
                failures.append(failure)

    if failures and _parse_bool(_prop(props, "failOnPartialFailure", "FailOnPartialFailure", default="false"), "failOnPartialFailure"):
        raise RuntimeError(f"failed to reconcile {len(failures)} of {len(targets)} discovered clusters: {failures[:3]}")

    if delete_request:
        for target in targets:
            _delete_worker_function(target)

    return {
        "success": len(failures) == 0,
        "debugAccessEnabled": debug_access_enabled,
        "discoveredClusterCount": len(targets),
        "reconciledClusterCount": len(results),
        "failureCount": len(failures),
        "results": results,
        "failures": failures,
    }


def _is_k8s_auth_error(error: Exception) -> bool:
    message = str(error)
    return "HTTP Error 401" in message or "HTTP Error 403" in message


def _lambda_execution_role_arn(props):
    role_arn = _prop(props, "lambdaExecutionRoleArn", "LambdaExecutionRoleArn", default=os.environ.get("LAMBDA_EXECUTION_ROLE_ARN", ""))
    if not role_arn:
        role_arn = _current_execution_role_arn(os.environ.get("AWS_REGION", "us-east-1"))
    return role_arn


def _verify_cluster_admin_access(targets, props):
    role_arn = _lambda_execution_role_arn(props)
    failures = []

    for target in targets:
        try:
            _verify_cluster_admin_access_entry(target["clusterName"], target["clusterRegion"], role_arn)
        except Exception as e:
            failure = {
                "cluster": target.get("clusterName"),
                "region": target.get("clusterRegion"),
                "error": str(e),
            }
            print(json.dumps({
                "message": "skipping cluster because bootstrap-owned EKS access entry is not ready",
                "roleArn": role_arn,
                **failure,
            }))
            failures.append(failure)
    return failures


def worker_handler(event, context):
    return _handle_worker_target(_normalize_properties(event))


def handler(event, context):
    try:
        if event.get("ResponseURL"):
            result = _queue_controller_from_cfn(event, context)
        else:
            result = _handle_controller(event, context)
        _send_cfn_response(event, context, "SUCCESS", _cfn_summary(result))
        return result
    except Exception as e:
        _send_cfn_response(event, context, "FAILED", {}, str(e))
        raise
