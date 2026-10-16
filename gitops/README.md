# GitOps on hub1 - first steps with Argo CD

Red Hat OpenShift GitOps (Argo CD) on hub1, deploying one application,
Online Boutique without its load generator, from git. Start here to learn
the GitOps loop before it deploys anything bigger, such as the hosted
clusters of [kubevirt/](../kubevirt/).

Each step says what a customer's production setup does differently, so the
lab shortcuts are visible.

## Contents

- [The idea in one paragraph](#the-idea-in-one-paragraph)
- [What is here](#what-is-here)
- [1. Install the operator](#1-install-the-operator)
- [2. Log in to Argo CD](#2-log-in-to-argo-cd)
- [3. The namespace, from the platform admin](#3-the-namespace-from-the-platform-admin)
- [4. The Application, and the first sync](#4-the-application-and-the-first-sync)
- [Exercises](#exercises)
- [Clean up](#clean-up)
- [What production does differently](#what-production-does-differently)

## The idea in one paragraph

Git holds the **desired state**. Argo CD compares it with the cluster's
**live state**, shows the difference as *OutOfSync*, and on a **sync** makes
the cluster match git. Nobody runs `oc apply` for the app any more. A change
is a commit, and a change made by hand on the cluster is *drift*, which Argo CD
shows, or with self-heal undoes. An **Application** is the object that ties
it together: a source (repo, revision, path), a destination (cluster,
namespace) and a sync policy.

## What is here

| File | Applied by | What |
|---|---|---|
| `bootstrap/01-openshift-gitops-operator.yaml` | you, once | The operator. It starts the default Argo CD instance in `openshift-gitops` |
| `apps/online-boutique-namespace.yaml` | you, as platform admin | The `online-boutique` namespace, labelled so that instance may deploy into it |
| `apps/online-boutique.yaml` | you, once | The Argo CD Application. From here on, Argo CD deploys the app |

The app itself comes from
[sadiquepp/openshift `test-workloads/online-boutique`](https://github.com/sadiquepp/openshift/tree/main/test-workloads/online-boutique),
pinned to a commit: 11 Deployments, about 1.5 CPU / 1.3 GiB, no PVCs.

## 1. Install the operator

```bash
export KUBECONFIG=/var/lib/libvirt/images/hub_install/auth/kubeconfig   # hub1
cd /root/ocp-onpremise
oc apply -f gitops/bootstrap/01-openshift-gitops-operator.yaml

oc get csv -n openshift-gitops-operator -w        # openshift-gitops-operator... Succeeded
oc get pods -n openshift-gitops                   # the default instance: server, repo-server, application-controller, redis, dex ... Running
oc get argocd -n openshift-gitops                 # openshift-gitops
```

The operator starts one Argo CD instance in `openshift-gitops` by itself. It
is the **default cluster-scoped instance**: it may manage some cluster-wide
configuration, and a namespace only once that namespace allows it (step 3).

## 2. Log in to Argo CD

```bash
oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='https://{.spec.host}{"\n"}'
```

Open it and choose **Log in via OpenShift**, as `kubeadmin` or any user in the
`cluster-admins` group. The default instance maps OpenShift's cluster
administrators to Argo CD's admin role. Its local `admin` user also works, as a
fallback:

```bash
oc extract secret/openshift-gitops-cluster -n openshift-gitops --keys=admin.password --to=-
```

The OpenShift console also gets a link: the application launcher (the grid
icon at the top) > **Cluster Argo CD**.

## 3. The namespace, from the platform admin

```bash
oc apply -f gitops/apps/online-boutique-namespace.yaml
oc get rolebinding -n online-boutique | grep openshift-gitops   # the operator's grant: admin, here only
```

The label `argocd.argoproj.io/managed-by: openshift-gitops` is the permission
model. The operator answers it by giving the instance's application
controller the `admin` role in this namespace, and nowhere else. Without the
label, the sync in step 4 fails with `forbidden`. That separation is
deliberate: the platform team decides where an Argo CD instance may deploy,
and the Application only fills the space it was given.

## 4. The Application, and the first sync

```bash
oc apply -f gitops/apps/online-boutique.yaml
oc get application online-boutique -n openshift-gitops      # SYNC STATUS OutOfSync, HEALTH Missing
```

`oc apply` warns `metadata.finalizers: "resources-finalizer.argocd.argoproj.io":
prefer a domain-qualified finalizer name including a path (/)`. That is the
Kubernetes API's generic advice for finalizer names without a `/`, and is
harmless. The name is Argo CD's documented one, and Argo CD honours it.

*OutOfSync / Missing* is the honest starting point: git describes 11
Deployments, Services and a Route, and the cluster has none of them. In the
Argo CD UI, open **online-boutique**: the tree shows every resource from git,
each one missing.

Press **Sync**, then **Synchronize**. Or, without the UI:

```bash
oc patch application online-boutique -n openshift-gitops --type=merge \
  -p '{"operation":{"initiatedBy":{"username":"admin"},"sync":{}}}'

oc get application online-boutique -n openshift-gitops -w   # Synced; HEALTH Progressing -> Healthy
oc get pods -n online-boutique                               # 11 Running, no loadgenerator
oc get route frontend -n online-boutique -o jsonpath='https://{.spec.host}{"\n"}'
```

*Healthy* is Argo CD's own judgement, from each resource's status: for
example, Deployments with their replicas available. It is separate from
*Synced*: a resource can match git and still be unhealthy.

## Exercises

Each exercise teaches one GitOps behaviour. Watch them in the UI.

**1. Drift: a change made by hand.**

```bash
oc scale deployment/frontend -n online-boutique --replicas=3
```

The Application turns *OutOfSync*. **App Diff** shows `replicas: 1` in git
against `3` on the cluster. Nothing reverts it yet, because sync is manual.
Press **Sync**: it goes back to 1. Git wins.

**2. A change through the desired state.**
Edit `gitops/apps/online-boutique.yaml` to run two frontends. Add under
`spec.source.kustomize`:

```yaml
      replicas:
        - name: frontend
          count: 2
```

Then `oc apply -f gitops/apps/online-boutique.yaml`, and Sync. This changes
the Application itself. In full GitOps the Application also lives in git, so
the change would be a commit that Argo CD picks up by itself. That is the next
lesson (app of apps).

**3. Self-heal: let Argo CD undo drift by itself.**
Add to the Application's `syncPolicy`, and apply:

```yaml
    automated:
      selfHeal: true
```

```bash
oc scale deployment/frontend -n online-boutique --replicas=5
oc get deployment frontend -n online-boutique -w     # back to the git value within seconds
oc delete deployment/checkoutservice -n online-boutique
oc get deployment checkoutservice -n online-boutique -w   # recreated
```

**4. Prune: removing something from git.**
Add a third `$patch: delete` patch to the Application, for `Deployment`
`adservice`. That removes it from the desired state. Then apply. With
`automated` but no `prune`, Argo CD reports `adservice` as *needs pruning* and
leaves it running. Add `prune: true` under `automated`, and it is deleted.
Prune is off by default because deleting things is the one mistake that is
hard to undo.

## Clean up

```bash
oc delete application online-boutique -n openshift-gitops   # the finalizer deletes everything it deployed
oc get all -n online-boutique                                # empty
oc delete -f gitops/apps/online-boutique-namespace.yaml
```

The Application carries `resources-finalizer.argocd.argoproj.io`, so deleting
it deletes the app. Without the finalizer, deleting an Application only stops
Argo CD from tracking the resources; they keep running, unmanaged.

The operator stays: the next lessons use it.

## What production does differently

| Here | Production | Why |
|---|---|---|
| Applications applied with `oc apply` | Only one root Application is bootstrapped, and it points at a directory of Applications in git (app of apps), or an ApplicationSet generates them | Every change, including adding an app, is then a reviewed commit |
| The app's source is someone else's repo, pinned to a commit | An environment repo the team owns, referencing upstream at a pinned tag or commit, with per-environment overlays (dev/stage/prod) | The team controls and reviews what reaches each environment |
| Project `default` | One `AppProject` per team, restricting `sourceRepos`, `destinations` and which kinds may be created | Without that, the default project lets any Application deploy anywhere the controller can reach; Red Hat recommends locking it down |
| The default instance deploys an app | The default instance stays the platform team's, for cluster configuration. App teams get their own namespace-scoped instances, or AppProjects | Separates who can change the platform from who can change apps |
| Least privilege via the `managed-by` label | The same, or tighter custom roles (the operator's `CONTROLLER_CLUSTER_ROLE`). Not cluster-admin | This repo's older `roles/setup-gitops` grants the controller cluster-admin: quick, and exactly what production avoids |
| `channel: latest`, automatic upgrades | A versioned channel, `installPlanApproval: Manual` | Operator upgrades are planned changes |
| Login as kubeadmin or local admin | SSO groups (OpenShift OAuth or Dex with the corporate IdP) mapped to Argo CD roles; local admin disabled | Audit trail per person |
| Manual sync, then self-heal | Automated sync with self-heal for platform config; apps often automated in dev, gated (manual sync, sync windows, or promotion by PR) in prod | Change control without hand-applied drift |
