"""Helm 动作的授权策略引擎。

为什么 Helm 需要单独一层策略，而不是直接用集群 RBAC：Helm 的回滚在 K8s 里表现为
对一堆 Deployment / Pod 的 patch，权限模型是「能不能改这些对象」，而不是
「能不能回滚这个 release」。集群 RBAC 根本无法表达「只允许回滚 staging 的 web」
这种意图——它连 release 这个概念都不认识。所以这一层必须自己实现。
"""
