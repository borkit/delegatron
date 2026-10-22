# Third-Party Licenses

Delegatron is licensed under the GNU Affero General Public License v3.0 (see
[LICENSE](LICENSE)). It uses the following open-source software; all licenses
listed are permissive and compatible with the AGPL-3.0.

## Python Dependencies

### Core dependencies

| Package | License | Purpose |
|---------|---------|---------|
| [openai](https://github.com/openai/openai-python) | Apache 2.0 | OpenAI API client + OpenAI-compatible endpoints (Ollama) |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | Data validation and schema |
| [click](https://github.com/pallets/click) | BSD 3-Clause | CLI framework |
| [rich](https://github.com/Textualize/rich) | MIT | Terminal formatting |
| [httpx](https://github.com/encode/httpx) | BSD 3-Clause | HTTP client |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD 3-Clause | Environment variable loading |
| [gitpython](https://github.com/gitpython-developers/GitPython) | BSD 3-Clause | Git operations |
| [fastapi](https://github.com/tiangolo/fastapi) | MIT | REST API server |
| [uvicorn](https://github.com/encode/uvicorn) | BSD 3-Clause | ASGI server |
| [tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack) | MIT | Code parsing for repo mapping |

### Optional dependencies

| Package | License | Purpose |
|---------|---------|---------|
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | MIT | Anthropic Claude API client |
| [google-genai](https://github.com/googleapis/python-genai) | Apache 2.0 | Google Gemini API client |
| [e2b-code-interpreter](https://github.com/e2b-dev/E2B) | Apache 2.0 | E2B sandbox (microVM) |
| [datasets](https://github.com/huggingface/datasets) | Apache 2.0 | SWE-bench dataset loading |

### Development dependencies

| Package | License |
|---------|---------|
| [pytest](https://github.com/pytest-dev/pytest) | MIT |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache 2.0 |
| [pytest-cov](https://github.com/pytest-dev/pytest-cov) | MIT |
| [ruff](https://github.com/astral-sh/ruff) | MIT |
| [mypy](https://github.com/python/mypy) | MIT |
| [pre-commit](https://github.com/pre-commit/pre-commit) | MIT |

## Dashboard Dependencies (Node.js)

### Core framework

| Package | License | Purpose |
|---------|---------|---------|
| [next](https://github.com/vercel/next.js) | MIT | React framework |
| [react](https://github.com/facebook/react) | MIT | UI library |
| [react-dom](https://github.com/facebook/react) | MIT | React DOM renderer |
| [typescript](https://github.com/microsoft/TypeScript) | Apache 2.0 | Type system |
| [tailwindcss](https://github.com/tailwindlabs/tailwindcss) | MIT | CSS framework |
| [eslint](https://github.com/eslint/eslint) | MIT | Linter |

## License Terms

### MIT License
Allows use, modification, distribution, and private use. Requires preservation
of copyright notice. No warranty. Compatible with commercial use.

### Apache License 2.0
Similar to MIT but includes an explicit patent grant. Requires preservation of
copyright and license notices. Compatible with commercial use.

### BSD 3-Clause License
Similar to MIT. Additionally prohibits using the copyright holder's name to
endorse derived products. Compatible with commercial use.

## Acknowledgements

This project stands on the shoulders of giants. Thank you to all the
maintainers and contributors of the above libraries.

## Updates

This file was last updated on 2026-04-16. Dependencies may have changed since
then. To regenerate this list:

```bash
pip install pip-licenses
python -m piplicenses --format=markdown > deps.md
```
