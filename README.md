# Mirror packages

## Docker Images
- https://hub.docker.com/_/busybox (tags: 1.37.0, 1.31.1)
- https://hub.docker.com/_/alpine (tags: 3.23.3)
- https://hub.docker.com/_/nginx (tags: 1.25, 1.25-alpine, 1.26, 1.26-alpine, 1.27, 1.27-alpine)
- https://hub.docker.com/_/postgres (tags: 15-alpine, 17, 18.3)
- https://hub.docker.com/_/redis → `official-redis` (tags: 7.2, 7.4)
- https://hub.docker.com/r/alpine/kubectl (tags: 1.35.3)
- https://hub.docker.com/r/amazon/aws-cli (tags: 2.34.5)
- https://hub.docker.com/r/bitnami/postgresql → `bitnami-postgresql` (tags: latest)
- https://hub.docker.com/r/bitnamisecure/postgresql → `bitnamisecure-postgresql` (tags: latest)
- https://hub.docker.com/r/bitnamisecure/redis → `bitnamisecure-redis` (tags: latest)
- https://hub.docker.com/r/bitnamisecure/redis → `redis` (tags: latest) *(legacy)*
- https://hub.docker.com/r/dothebetter/rsync → `rsync` (tags: 3.4.1)
- https://hub.docker.com/r/grafana/grafana (tags: 11.4.0, 12.3.1)
- https://hub.docker.com/r/grafana/grafana-image-renderer (tags: latest)
- https://hub.docker.com/r/nginxdemos/hello → `nginxdemos-hello` (tags: 0.4)
- https://hub.docker.com/r/omnistrate/noop → `noop` (tags: latest)
- https://hub.docker.com/r/redis/redis-stack-server → `redis-stack-server` (tags: 6.2.6-v7)
- https://hub.docker.com/r/tomnislav/pg-with-exporter → `pg-with-exporter` (tags: 2.0)
- https://hub.docker.com/r/temporalio/auto-setup → `temporalio-auto-setup` (tags: 1.29.0)
- https://hub.docker.com/r/temporalio/ui → `temporalio-ui` (tags: 2.39.0)
- https://hub.docker.com/r/testcontainers/ryuk → `testcontainers-ryuk` (tags: 0.13.0)
- https://gallery.ecr.aws/eks/aws-load-balancer-controller → `aws-load-balancer-controller` (tags: v2.17.1)
- https://gallery.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver → `aws-ebs-csi-driver` (tags: v1.55.0)
- https://gallery.ecr.aws/csi-components/csi-attacher → `csi-attacher` (tags: v4.10.0-eksbuild.3)
- https://gallery.ecr.aws/csi-components/csi-provisioner → `csi-provisioner` (tags: v6.1.0-eksbuild.2)
- https://gallery.ecr.aws/csi-components/csi-resizer → `csi-resizer` (tags: v2.0.0-eksbuild.3)
- https://gallery.ecr.aws/csi-components/csi-node-driver-registrar → `csi-node-driver-registrar` (tags: v2.15.0-eksbuild.3)
- https://gallery.ecr.aws/csi-components/livenessprobe → `csi-livenessprobe` (tags: v2.17.0-eksbuild.3)
- https://gallery.ecr.aws/efs-csi-driver/amazon/aws-efs-csi-driver → `aws-efs-csi-driver` (tags: v2.3.0)
- https://gallery.ecr.aws/eks-distro/kubernetes-csi/livenessprobe → `eks-distro-csi-livenessprobe` (tags: v2.12.0-eks-1-29-7)
- https://gallery.ecr.aws/eks-distro/kubernetes-csi/node-driver-registrar → `eks-distro-csi-node-driver-registrar` (tags: v2.10.0-eks-1-29-7)
- https://gallery.ecr.aws/mountpoint-s3-csi-driver/aws-mountpoint-s3-csi-driver → `aws-mountpoint-s3-csi-driver` (tags: v2.3.0)

## Helm Charts
All at `oci://ghcr.io/omnistrate/charts/`:
- `redis` 20.0.0, 20.1.0, 20.2.0, 22.0.7 from `oci://registry-1.docker.io/bitnamicharts`
- `postgresql` 16.7.27 from `oci://registry-1.docker.io/bitnamicharts`
- `nginx` 18.0.0, 18.1.0 from `oci://registry-1.docker.io/bitnamicharts`
- `postgres` 0.18.3 from `oci://registry-1.docker.io/cloudpirates`
- `common` 2.2.0 from `oci://registry-1.docker.io/cloudpirates`

Created to avoid throttling from public upstream registries.
