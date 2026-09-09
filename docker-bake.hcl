# Multi-arch images (linux/amd64 + linux/arm64).
# Local Mac: prefer `docker compose up --build` (native arch only).
# Multi-arch:  `docker buildx bake` then push, or load with a containerd store.
#
#   docker buildx create --name multi --use   # once
#   docker buildx bake --push                 # needs IMAGE_PREFIX registry
#   IMAGE_PREFIX=ghcr.io/you/ docker buildx bake --push

variable "TAG" {
  default = "latest"
}

variable "IMAGE_PREFIX" {
  default = ""
}

variable "MODEL_NAME" {
  default = "granite4.1:8b"
}

function "tags" {
  params = [name]
  result = compact([
    IMAGE_PREFIX != "" ? "${IMAGE_PREFIX}${name}:${TAG}" : "${name}:${TAG}",
  ])
}

group "default" {
  targets = ["site", "model"]
}

target "site" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = tags("chatbot-site")
  platforms  = ["linux/amd64", "linux/arm64"]
}

target "model" {
  context    = "./model"
  dockerfile = "Dockerfile"
  tags       = tags("chatbot-model")
  platforms  = ["linux/amd64", "linux/arm64"]
  args = {
    MODEL_NAME = MODEL_NAME
  }
}
