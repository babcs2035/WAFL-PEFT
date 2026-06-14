#!/usr/bin/env bash
# analyze:collect のためのログ回収スクリプト
set -euo pipefail

DEPLOY_DIR="/home/denjo/workspace/ktakahashi/WAFL-PEFT"
SSH_USER="denjo"
SERVER_HOST="wafl-ctrl1"

# 最新の実験ディレクトリを自動取得（最終更新日が最新）
EXPERIMENT_DIR=$(ssh "$SSH_USER@$SERVER_HOST" "ls -1t $DEPLOY_DIR/results/ | head -1")

# メタファイルを保存
mkdir -p results
python3 -c "import json; open('results/.experiment_meta.json','w').write(json.dumps({'dir_name':'$EXPERIMENT_DIR'}))"

# ログ回収（最新ディレクトリを対象）
ssh "$SSH_USER@$SERVER_HOST" "cd $DEPLOY_DIR && python3 src/collect_logs.py" > /tmp/collect_output.txt 2>&1

# ローカルにコピー
rsync -az "$SSH_USER@$SERVER_HOST:$DEPLOY_DIR/results/$EXPERIMENT_DIR/" "results/$EXPERIMENT_DIR/"
