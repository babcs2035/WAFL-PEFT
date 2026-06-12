FROM python:3.10-bookworm

# 1. 変更頻度が低いシステム依存（ほとんど再ビルドされない）
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# 2. uvのインストール（ほぼ不変）
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# 3. 仮想環境の作成（不変）
RUN uv venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 4. Pythonパッケージインストール（requirementsが変わった時のみ再ビルド）
RUN uv pip install --extra-index-url https://download.pytorch.org/whl/cpu \
    torch torchvision torchaudio transformers accelerate huggingface_hub datasets peft torchinfo tensorboard 'pyarrow<15' 'numpy<2'

# 5. 変更頻度が高いソースコード（ここから下のみ再ビルドされる）
COPY src/ /app/src/
COPY config/ /app/config/

# 6. Dockerインセキュアレジストリ設定
RUN mkdir -p /etc/docker && echo '{"insecure-registries": ["127.0.0.1:5000", "localhost:5000"]}' > /etc/docker/daemon.json

# 7. ログ出力ディレクトリの作成
RUN mkdir -p /app/logs

CMD ["python", "src/client.py"]
