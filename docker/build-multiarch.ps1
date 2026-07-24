param(
    [string]$Image = "photosweep:local"
)

$ErrorActionPreference = "Stop"
docker buildx build `
    --platform linux/amd64,linux/arm64 `
    --tag $Image `
    --output "type=oci,dest=photosweep-multiarch.tar" `
    .
