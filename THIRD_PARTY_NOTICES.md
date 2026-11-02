# Third-party notices

The project MIT license applies to DFX Agent code; it does not replace dependency licenses.

| Dependency | Version pinned by this release | License |
|---|---|---|
| mini-swe-agent | 2.4.6 | MIT; [upstream notice](third_party/mini-swe-agent-LICENSE.md) |
| tokenizers | 0.23.3 | Apache-2.0 |
| FastAPI | 0.162.0 | MIT |
| Uvicorn | 0.54.0 | BSD-3-Clause |

Dependency packages are installed through pip and are not vendored here. Their own distributions contain the applicable notices. The pinned requirements include transitive dependencies with their own licenses.

The DeepSeek tokenizer is downloaded separately from https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip by scripts/prepare-tokenizer.py. This repository does not redistribute the tokenizer asset or grant rights to it. Its use and the model API remain subject to the provider's applicable terms.

No application datasets, device images, SDKs, signing material or historical execution records are included.
