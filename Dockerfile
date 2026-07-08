FROM python:3.10-bookworm

# 1. 変更頻度が低いシステム依存（ビルド時のみ root で実行）
RUN apt-get update && apt-get install -y --no-install-recommends curl rsync && rm -rf /var/lib/apt/lists/*

# 2. uvのインストール（ビルド時のみ root で実行）
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# 3. 一般ユーザーの作成（--build-arg で ssh_user を指定）
ARG ssh_user=denjo
RUN groupadd -g 1000 $ssh_user && \
    useradd -u 1000 -g $ssh_user -m -s /bin/bash $ssh_user

WORKDIR /app

# 4. pyproject.tomlをコピーして依存関係を解決
COPY pyproject.toml /app/pyproject.toml

# 5. 仮想環境の作成＋パッケージインストール（root で実行）
RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install --extra-index-url https://download.pytorch.org/whl/cu128 \
    torch torchvision torchaudio peft torchinfo tensorboard 'pyarrow<15' 'numpy<2' && \
    uv pip install bitsandbytes && \
    uv pip install 'git+https://github.com/huggingface/transformers.git' 'git+https://github.com/huggingface/accelerate.git' 'git+https://github.com/huggingface/datasets.git'
ENV PATH="/app/.venv/bin:$PATH"

# 6. ソースコードをコピーし、所有権を一般ユーザーに移行
COPY src/ /app/src/
COPY config/ /app/config/
RUN chown -R $ssh_user:$ssh_user /app

# 7. Dockerインセキュアレジストリ設定（ビルド時のみ root で実行）
RUN mkdir -p /etc/docker && echo '{"insecure-registries": ["127.0.0.1:5000", "localhost:5000"]}' > /etc/docker/daemon.json

# 8. ログ出力ディレクトリの作成（一般ユーザーが書き込めるように所有権を設定）
RUN mkdir -p /app/logs && chown $ssh_user:$ssh_user /app/logs

# 9. 一般ユーザーに切り替え
USER $ssh_user

CMD ["python3", "src/client.py"]
