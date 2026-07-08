#!/usr/bin/env bash
# analyze:collect のためのログ回収スクリプト
# config/settings.json から設定値を読み込み、管理サーバーからログを回収する。
set -euo pipefail

SETTINGS="config/settings.json"

DEPLOY_DIR=$(python3 -c "import json; c=json.load(open('$SETTINGS')); print(c['deployment']['deploy_dir'])")
SSH_USER=$(python3 -c "import json; c=json.load(open('$SETTINGS')); print(c['deployment']['ssh_user'])")
SERVER_HOST=$(python3 -c "import json; c=json.load(open('$SETTINGS')); print(c['server']['server_host'])")

# .experiment_meta.json から実験ディレクトリ名を取得
EXPERIMENT_DIR=$(ssh "$SSH_USER@$SERVER_HOST" "cat $DEPLOY_DIR/results/.experiment_meta.json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dir_name',''))")

# メタファイルをローカルへ保存
mkdir -p results
echo "{\"dir_name\":\"$EXPERIMENT_DIR\"}" > results/.experiment_meta.json

# ログ回収（最新ディレクトリを対象）
ssh "$SSH_USER@$SERVER_HOST" "cd $DEPLOY_DIR && python3 src/collect_logs.py" > /tmp/collect_output.txt 2>&1

# ローカルにコピー
rsync -az "$SSH_USER@$SERVER_HOST:$DEPLOY_DIR/results/$EXPERIMENT_DIR/" "results/$EXPERIMENT_DIR/"
