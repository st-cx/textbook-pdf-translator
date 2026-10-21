# 可选的 GitHub Actions 自动检查

源码和本地检查记录已包含于仓库。发布时当前 GitHub OAuth 凭据不具备 `workflow` scope，GitHub 明确拒绝推送 `.github/workflows/tests.yml`。因此本版本**未启用或运行 GitHub Actions，不能声称托管CI通过**。

具备相应仓库权限与 workflow 授权后，可在GitHub网页或正常授权的Git环境中将下列内容保存为 `.github/workflows/tests.yml`。这是建议配置，尚未在GitHub runner执行验证；首次运行若暴露环境差异，应修正后再采用。它只运行公开合成/合同测试，不调用付费翻译API或下载教材。

```yaml
name: Public source checks
on:
  push:
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  windows-tests:
    runs-on: windows-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
          cache: pip
          cache-dependency-path: requirements-lock.txt
      - name: Install locked dependencies
        run: python -m pip install -r requirements-lock.txt
      - name: Install official pinned Typst
        run: python scripts/install_typst.py
      - name: CLI smoke check
        run: python translate_pdf.py --help
      - name: Synthetic and contract tests (no provider calls)
        run: python -m unittest discover -s tests -v
```

已执行的公开本地检查见 [VALIDATION.md](VALIDATION.md) 与 [回执](../evidence/public-source-tests.json)。建议流程安装锁定依赖和官方Typst，需要GitHub runner能访问公开软件分发地址。
