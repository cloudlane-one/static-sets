# How to verify the disconnected reboot guarantee

The guarantee is that kubelet restarts the last installed revision after a host
reboot without Kubernetes API access. That cannot be verified from a controller test:
it needs a host that boots, a kubelet that reads a file, and no API server reachable.
This page is the reproducible procedure.

**What CI verifies** (`STATIC_SETS_K8S_INTEGRATION=1`, see `tests/test_e2e.py`): on a
real cluster, a StaticSet is placed by the scheduler, the node's manager compiles the
workload, materializes its configuration, writes the manifest into kubelet's own
directory, acknowledges the placement, and kubelet publishes the mirror Pod for what it
started — then the whole thing is removed again on cleanup. The suite pins the workload
to one node, so it can run against a kind cluster whose control plane is itself tagged
with static Pods, without ever touching one of them.

**What CI does not verify:** that a rebooted kubelet starts that manifest. That is
this procedure.

## Prerequisites

- A Linux host with kubelet installed and a container runtime, able to join a
  cluster and then be disconnected from it.
- An image the workload uses, already present on that host.
- `kubectl` access to the cluster while it is connected.

## Procedure

### 1. Install with an explicit static Pod path

Use a StaticSet whose `staticPodPath` matches the host's real kubelet configuration
(`kubelet --pod-manifest-path`, or `staticPodPath` in the kubelet config file). Do not
guess it: the operator never edits kubelet configuration, so a mismatch is a silent
no-op at the kubelet level.

```shell
# The manager's paths are the node's, so they are chart values, not StaticSet fields.
helm get values static-sets -n static-sets | grep -A3 manager:
# manager:
#   staticPodPath: /etc/kubernetes/manifests   # must match kubelet
#   bundleDir: /var/lib/static-sets
```

```shell
kubectl apply -f reboot-test.yaml
```

### 2. Confirm the acknowledged install

```shell
kubectl get staticset reboot-test -o jsonpath='{.status.placements[*].revision}{"\n"}'
kubectl get staticset reboot-test -o jsonpath='{.status.placements[*].acknowledged}{"\n"}'
```

Both must be non-empty and `true`. An unacknowledged placement means the node's manager
never finished, and a reboot test on top of it proves nothing.

### 3. Record what the node has, without the cluster

On the host:

```shell
ls -l /etc/kubernetes/manifests/
cat /etc/kubernetes/manifests/reboot-test-*.yaml
ls -l /var/lib/static-sets/placements/*/revisions/*/
cat /var/lib/static-sets/placements/*/ownership.json
```

Save those outputs. They are the baseline you will compare against after the reboot.

### 4. Disconnect the node

Stop or block the node's access to the API server, for example:

```shell
sudo iptables -I OUTPUT -p tcp --dport 6443 -j REJECT
sudo iptables -I INPUT -p tcp --sport 6443 -j REJECT
```

Also make sure the mirror Pod's record disappears from the API server's point of
view (the node will go `NotReady`); that is expected and is not a failure.

> Do not simply stop kubelet. The guarantee is about a reboot, and a reboot restarts
> kubelet; stopping it only tests the operator's offline behaviour, which is a
> different (also covered) claim.

### 5. Reboot the host

```shell
sudo systemctl reboot
```

### 6. Verify, still disconnected

Back on the host, before restoring the network:

```shell
# The manifest kubelet reads must be unchanged.
diff <(cat /etc/kubernetes/manifests/reboot-test-*.yaml) <your saved copy>

# kubelet must have started it, with no API server involved.
sudo crictl pods | grep reboot-test
sudo crictl ps | grep reboot-test
journalctl -u kubelet --since "10 min ago" | grep -i static
```

Expected: the manifest is byte-identical, the containers are running, and the
workload's own logs show it came up. If the containers are running but the app is not
healthy, check the image is present locally and the app's dependencies are reachable —
that part is outside the guarantee, as
[described here](./about-static-pods.md#what-that-depends-on).

### 7. Restore the connection

```shell
sudo iptables -D OUTPUT -p tcp --dport 6443 -j REJECT
sudo iptables -D INPUT -p tcp --sport 6443 -j REJECT
```

Give the node a minute to go `Ready`, then check that the operator reconnected and
did **not** change anything:

```shell
kubectl get staticset reboot-test -o yaml | grep -A3 placements
```

`desiredRevision` must equal the revision recorded in step 2. If it changed, the
operator computed a different revision offline than it had online, which is a bug:
revisions are a pure function of the template and the resolved configuration.

### 8. Clean up

```shell
kubectl delete -f reboot-test.yaml
```

## Also worth testing while you are here

- **Reinstall after a reboot.** Delete the StaticSet while connected, reboot, and
  confirm the manifest is gone and nothing starts. This is the cleanup protocol, not
  the reboot guarantee.
- **Disconnected revision retention.** While disconnected, the last committed
  revision must stay active. `spec.offline.retainedRevisions` keeps older revisions on
  the node so a failed update can be rolled back; the manager never prunes the
  active one.
- **Image absence.** With `spec.offline.imageProvisioning: VerifiedBeforeInstall`, a
  revision whose images are not confirmed present is refused, so you cannot install
  something that will fail after the reboot.

## Reporting what you observed

If you run this, record the kubelet version, the container runtime, the image
reference, the revision before and after, and the exact commands. Whether a reboot
guarantee holds is a property of a specific node configuration; a version without
that context is not evidence.