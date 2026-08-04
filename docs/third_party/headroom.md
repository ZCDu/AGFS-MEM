# Headroom external service

DREAM integrates Headroom only as a replaceable HTTP service. It does not copy,
package, import, or execute Headroom source code, Kompress models, ONNX Runtime or
Headroom Python dependencies.

## Upstream and deployment identity

- Repository: <https://github.com/headroomlabs-ai/headroom>
- Documentation: <https://headroom-docs.vercel.app/docs/proxy>
- Isolated tool distribution: `headroom-ai[all]==0.33.0`
- Upstream license: Apache License 2.0
- Health endpoint: `GET /health`
- Background compression endpoint: `POST /v1/compress`
- Agent model paths: OpenAI/Anthropic-compatible Headroom Proxy endpoints

Install Headroom outside the DREAM `.venv`:

```bash
uv tool install --python 3.13 "headroom-ai[all]==0.33.0"
```

Start the official service without forcing a compressor or ratio:

```bash
HEADROOM_CCR_TTL_SECONDS=43200 headroom proxy \
  --host 127.0.0.1 \
  --port 8787 \
  --mode token
```

DREAM decides only when its three PLAN thresholds require background compression.
Headroom owns ContentRouter selection, Kompress and other compressors, CCR cache,
markers, `headroom_retrieve`, relevance decisions and supported model continuation.
DREAM preserves Headroom messages as opaque protocol objects.

Because no Headroom code or model artifact is redistributed, DREAM has no vendored
Headroom license/source tree or ML dependency. Another service can replace Headroom
by implementing the same `CompressionClient` HTTP contract and Agent proxy boundary.
