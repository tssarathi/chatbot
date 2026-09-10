# Deploy chatbot to the nuberu mgmt cluster (Cilium L2 LoadBalancer).

Prerequisites
- `KUBECONFIG` pointing at mgmt (e.g. `secrets/mgmt.kubeconfig`)
- Cilium LB IP pool covering `10.0.0.240` (`platform/cilium/l2-lb.yaml`)
- Both images reachable from the nodes. The registry and tag live in one place,
  the `images:` block of `kustomization.yaml`. Retarget with:
  `kustomize edit set image chatbot-site=ghcr.io/you/chatbot-site:v1`
- `imagePullPolicy` is `IfNotPresent`, so a re-pushed `:latest` is **not** picked up.
  Push a new tag and bump `newTag` instead.

Build & load image (lab nodes)
```sh
# Mac → in-cluster HTTP registry (port-forward + crane)
kubectl -n nuberu-factory port-forward svc/nuberu-factory-registry 5050:5000 &
docker buildx build --platform linux/amd64 -t chatbot-site:amd64 \
  --output type=docker,dest=/tmp/chatbot-site-amd64.tar .
crane push --insecure /tmp/chatbot-site-amd64.tar 127.0.0.1:5050/chatbot-site:latest

# Pre-pull into CRI on each Talos node (HTTP registry)
export TALOSCONFIG=/path/to/talos/mgmt/talosconfig
for n in 10.0.0.235 10.0.0.236 10.0.0.237; do
  talosctl -n "$n" image pull 10.101.191.154:5000/chatbot-site:latest --namespace cri
done
```

Apply
```sh
export KUBECONFIG=/path/to/mgmt.kubeconfig
kubectl apply -k deploy/k8s
kubectl -n chatbot rollout status deploy/chatbot
kubectl -n chatbot get svc chatbot
# → EXTERNAL-IP 10.0.0.240
open http://10.0.0.240/
```

Model endpoint
`OLLAMA_URL` defaults to `http://chatbot-model:11434`. Point it at a reachable
Ollama (Compose model, nodePort, or in-cluster Deployment) before chatting.

Platform detection (Environment intelligence)
`/whereami` prefers live cluster facts when the pod has a service account:
1. `PLATFORM` env if set (`onprem` | `rosa` | `eks`)
2. Else node labels / `providerID`:
   - `eks.amazonaws.com/*` → **EKS**
   - OpenShift labels + `aws://` provider → **ROSA**
   - OpenShift without AWS → **ONPREM**
   - bare `aws://` → **EKS**
   - otherwise → **ONPREM**
3. Region / zone from `topology.kubernetes.io/{region,zone}`
4. Node / pod / IP from the Downward API

UI mapping
| Platform | Currently serving from | K8s platform mark |
| --- | --- | --- |
| onprem | On-premises | OpenShift |
| rosa | ROSA + AWS logo | OpenShift |
| eks | EKS + AWS logo | EKS (orange K) |
