# syntax=docker/dockerfile:1
# RUN --mount=type=cache（uv/pipダウンロードキャッシュの永続化）を使うため
# BuildKit構文を明示指定する
FROM python:3.10-bookworm

# 1. 変更頻度が低いシステム依存（ビルド時のみ root で実行）
# openssh-client は global_eval.py が管理サーバーコンテナ内から各学習
# デバイスへ直接SSH+rsyncでチェックポイントを収集するために必要
RUN apt-get update && apt-get install -y --no-install-recommends curl rsync openssh-client && rm -rf /var/lib/apt/lists/*

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

# 5. 仮想環境の作成＋パッケージインストール（root で実行）。
#    --mount=type=cache でuv/pipのダウンロードキャッシュをビルドキャッシュとは
#    独立して永続化する。システム依存（レイヤー1）を変更してこれより上流の
#    レイヤーキャッシュが無効になった場合でも、torch等の巨大パッケージ（合計
#    10GB超）の再ダウンロードを避けられる（レイヤーキャッシュ無効化時に
#    600秒超かかっていたのが、ダウンロードキャッシュヒットで数十秒に短縮される）
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install --extra-index-url https://download.pytorch.org/whl/cu128 \
    torch torchvision torchaudio peft torchinfo tensorboard 'pyarrow<15' 'numpy<2' && \
    uv pip install bitsandbytes && \
    uv pip install 'git+https://github.com/huggingface/transformers.git' 'git+https://github.com/huggingface/accelerate.git' 'git+https://github.com/huggingface/datasets.git'
ENV PATH="/app/.venv/bin:$PATH"

# 6. 仮想環境の所有権を一般ユーザーへ変更。この時点では /app 配下は
#    .venv のみ（src/・config/ はまだCOPYしていない）なので、chown -R の
#    対象がここで確定し、以降src/・config/を何度変更してもこの層は
#    再実行されない
RUN chown -R $ssh_user:$ssh_user /app

# 7. ソースコードをコピー（--chownで直接所有権を指定）。src/・config/は
#    変更頻度が最も高いため、chown -R の再実行を伴わないこの形にすることで
#    ソース変更のみのリビルドを数百秒から数秒に短縮する
COPY --chown=$ssh_user:$ssh_user src/ /app/src/
COPY --chown=$ssh_user:$ssh_user config/ /app/config/

# 8. Dockerインセキュアレジストリ設定（ビルド時のみ root で実行）
RUN mkdir -p /etc/docker && echo '{"insecure-registries": ["127.0.0.1:5000", "localhost:5000"]}' > /etc/docker/daemon.json

# 9. ログ出力ディレクトリの作成（一般ユーザーが書き込めるように所有権を設定）
RUN mkdir -p /app/logs && chown $ssh_user:$ssh_user /app/logs

# 10. 一般ユーザーに切り替え
USER $ssh_user

CMD ["python3", "src/client.py"]
