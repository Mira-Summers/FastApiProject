from kubernetes import client,config
from kubernetes.client.rest import ApiException
import subprocess
from typing import List,Optional

def load_config():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
load_config()

core_v1 = client.CoreV1Api()

def list_pods(namespace: str = "default") -> list[dict]:
    try:
        pods = core_v1.list_pod_for_all_namespaces()
        result = []
        for pod in pods.items:
            restarts = 0
            if pod.status.container_statuses:
                restarts = sum(cs.restart_count for cs in pod.status.container_statuses)

            result.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "node": pod.spec.node_name,
                "restarts": restarts,
                "age": str(pod.metadata.creation_timestamp)
            })
        return result
    except ApiException as e:
        raise Exception(f"查询Pod失败：{e.reason}")

def get_pod_logs(pod_name: str,namespace: str = "default",tail_lines: int = 100)->List[str]:
    try:
        log = core_v1.read_namespaced_pod_log(
            name = pod_name,
            namespace = namespace,
            tail_lines=tail_lines
        )
        return logs.split("\n")
    except ApiException as e:
        raise Exception(f"获取日志失败：{e.reason}")

def helm_rollback(release_name: str,namespace: str = "default",revision: Optional[int]=None)->dict:
    """执行helm回滚"""
    cmd = ["helm","rollback",release_name,"-n",namespace]
    if revision is not None:
        cmd.append(str(revision))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text = True,
            timeout= 60
        )
        if result.returncode == 0:
            return {"success": True,"message":"回滚成功","output": result.stdout}
        else:
            return {"success": False,"message":"回滚失败","output": result.stderr}
    except subprocess.TimeoutExpired:
        return {"success": False,"message": "回滚超时(60秒)","output": None}
    except FileNotFoundError:
        return {"success": False,"message": "helm命令未找到，请确认已安装","output":None}

def get_helm_history(release_name: str,namespace: str = "default") -> list[dict]:
    cmd = ["helm","history",release_name,"-n",namespace,"-o","json"]
    try:
        result = subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        if result.returncode == 0:
            import json
            return json.loads(result.stdout)
        else:
            raise Exception(result.stderr)
    except Exception as e:
        raise Exception(f"查询Helm历史失败：{str(e)}")