#!/usr/bin/env bash
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
export AZURE_CORE_COLLECT_TELEMETRY=no

# Bootstrap is delivered by ARM, not by an unauthenticated network listener.
python3 - <<'PY'
import base64, json, os, re
c = json.loads(base64.b64decode(os.environ["COMMERCE_BUILD_CONFIG"], validate=True))
patterns = {
    "repository": r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+",
    "sha": r"[0-9a-f]{40}", "tag": r"cb-[a-z][a-z0-9-]{2,30}",
    "registry": r"[a-z0-9]{5,50}\.azurecr\.io",
    "registry_name": r"[a-z0-9]{5,50}", "location": r"[a-z]+[0-9]*",
    "builder_client_id": r"[0-9a-f-]{36}", "subscription_id": r"[0-9a-f-]{36}",
    "tenant_id": r"[0-9a-f-]{36}", "private_endpoint_cidr": r"[0-9./]+",
    "mode": r"smoke|retail-storefront|retail-release|all",
}
assert set(c) == set(patterns), "unexpected remote configuration"
assert all(isinstance(c[k], str) and re.fullmatch(p, c[k]) for k,p in patterns.items())
assert c["registry"] == c["registry_name"] + ".azurecr.io"
with open("/run/commerce-build.env", "x") as f:
    for k,v in c.items():
        f.write(f"{k.upper()}='{v}'\n")
PY
source /run/commerce-build.env

for path in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do
  if [ -f "$path" ]; then
    sed -i -E 's#http://([a-z0-9.-]*archive\.ubuntu\.com|security\.ubuntu\.com)/#https://\1/#g' "$path"
  fi
done
apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=180 update -qq
apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=180 install -y -qq ca-certificates curl gnupg docker.io git
curl --fail --silent --show-error https://packages.microsoft.com/keys/microsoft.asc |
  gpg --dearmor --yes --output /usr/share/keyrings/commerce-microsoft.gpg
chmod 0644 /usr/share/keyrings/commerce-microsoft.gpg
printf 'deb [arch=amd64 signed-by=/usr/share/keyrings/commerce-microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ noble main\n' \
  > /etc/apt/sources.list.d/commerce-azure-cli.list
chmod 0644 /etc/apt/sources.list.d/commerce-azure-cli.list
apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=180 update -qq
apt-get -o Acquire::Retries=3 -o DPkg::Lock::Timeout=180 install -y -qq azure-cli
systemctl enable --now docker

export REGISTRY LOCATION PRIVATE_ENDPOINT_CIDR
python3 - <<'PY'
import ipaddress, os, socket, ssl
host = os.environ["REGISTRY"]
subnet = ipaddress.ip_network(os.environ["PRIVATE_ENDPOINT_CIDR"])
for name in (host, host.split(".")[0] + "." + os.environ["LOCATION"] + ".data.azurecr.io"):
    addresses = {item[4][0] for item in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)}
    assert addresses and all(ipaddress.ip_address(a) in subnet for a in addresses), f"non-private ACR DNS: {name}"
    with socket.create_connection((name,443), timeout=15) as stream:
        with ssl.create_default_context().wrap_socket(stream, server_hostname=name):
            pass
    print(f"Private DNS/TLS verified: {name}", flush=True)
PY
az login --identity --client-id "$BUILDER_CLIENT_ID" --allow-no-subscriptions --output none
export DOCKER_CONFIG=/run/commerce-docker
mkdir -m 700 "$DOCKER_CONFIG"
az acr login --subscription "$SUBSCRIPTION_ID" --name "$REGISTRY_NAME" \
  --expose-token --query accessToken --output tsv |
  docker login "$REGISTRY" --username 00000000-0000-0000-0000-000000000000 --password-stdin

mkdir -m 700 /build
git init --quiet /build/source
git -C /build/source remote add origin "https://github.com/${REPOSITORY}.git"
git -C /build/source -c credential.helper= fetch --quiet --depth=1 origin "$SHA"
git -C /build/source checkout --quiet --detach FETCH_HEAD
test "$(git -C /build/source rev-parse HEAD)" = "$SHA"
cd /build/source
printf '{}\n' > /run/commerce-digests.json
build_image() {
  local image="$1" file="$2"
  shift 2
  local reference="${REGISTRY}/commerce/${image}:${SHA}-${TAG}"
  docker build --label "org.opencontainers.image.revision=${SHA}" \
    --label "org.opencontainers.image.source=https://github.com/${REPOSITORY}" \
    --file "$file" --tag "$reference" "$@" .
  local user
  user="$(docker image inspect --format '{{.Config.User}}' "$reference")"
  case "$user" in ""|root|0|0:*|root:*) echo "Image must be non-root" >&2; return 1;; esac
  if [ "$image" = gateway ]; then
    docker run --rm --entrypoint python "$reference" -c 'import commerce_gateway'
  elif [ "$image" = api ]; then
    docker run --rm --entrypoint python "$reference" -c \
      "import commerce_host, shopping_agent_runtime, merchant_agent_runtime, openai, azure.cosmos; from pathlib import Path; assert Path('/app/shopping-agent/skills').is_dir(); assert Path('/app/merchant-agent/skills').is_dir()"
  fi
  docker push "$reference"
  docker image inspect --format '{{json .RepoDigests}}' "$reference" |
    python3 -c 'import json,sys; p="/run/commerce-digests.json"; a=json.load(sys.stdin); prefix=sys.argv[2]+"@"; d=[x.split("@",1)[1] for x in a if x.startswith(prefix)]; assert len(d)==1; data=json.load(open(p)); data[sys.argv[1]]=d[0]; open(p,"w").write(json.dumps(data,separators=(",",":")))' \
      "$image" "${REGISTRY}/commerce/${image}"
}
if [ "$MODE" = retail-storefront ]; then
  build_image retail-storefront-web Dockerfile.web --build-arg VERTICAL=retail --build-arg WEB_APP=storefront-web
else
  build_image gateway Dockerfile.gateway
fi
if [ "$MODE" = retail-release ]; then
  build_image api Dockerfile.api
  build_image retail-storefront-web Dockerfile.web --build-arg VERTICAL=retail --build-arg WEB_APP=storefront-web
  build_image retail-merchant-web Dockerfile.web --build-arg VERTICAL=retail --build-arg WEB_APP=merchant-web
fi
if [ "$MODE" = all ]; then
  build_image api Dockerfile.api
  for vertical in retail travel telecom entertainment; do
    for app in storefront-web merchant-web; do
      build_image "${vertical}-${app}" Dockerfile.web --build-arg "VERTICAL=${vertical}" --build-arg "WEB_APP=${app}"
    done
  done
fi
docker logout "$REGISTRY"
printf 'COMMERCE_BUILD_RESULT=%s\n' "$(cat /run/commerce-digests.json)"
