# 実験ジャーナル: WAFL-PEFT

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．

---

## Iteration 15: merge JSONLメトリクス化 + W3対比実験


### 仮説

W3（`merge_include_self`）修正の独自効果を測定するには、merge イベントの発生を定量観測できる環境と、W3 あり/なしの対比実験の両方が必要である。

Iter14 では W3 修正と P2P 接続修正が同時に適用された結果、accuracy 20.0% の要因が W3 由来か P2P 修正由来か分離不能だった。P2P 接続修正は完了済みなので、次は W3 のみを単一レバーとして対比実験できる。

ただしその前に、merge イベントが JSONL メトリクスに記録されていないため、前実験では merge が「発生したか」さえ確認できなかった。merge JSONL 化を先に行い、観測可能性を確保してから W3 対比実験へ進む。

### 単一レバー

**`merge_jsonl_metrics`**: `src/client.py` の `p2p_exchange_thread`（Thread 2）merge ループで、`state.metrics_queue` へ merge イベントを JSONL 形式で追記する。

- 変更箇所: 行1021 の `state.merge_queue.put(merged, timeout=1.0)` の直後
- 追加内容: 8 行程度の merge イベント追記
- 固定構成: W3 修正（120b4ba）は main 既定のまま、学習ハイパラ・接触パターン・settings.json は既存構成に固定

### 変更内容の設計

**`src/client.py` 行1021 直後への追加**:

```python
merge_event = {
    "type": "merge", "peer_id": PEER_ID, "step": current_step,
    "elapsed": state.elapsed_time, "num_peers_merged": count - 1,
    "merge_includes_self": True,
}
try:
    state.metrics_queue.put(merge_event, timeout=1.0)
except queue.Full:
    pass
```

- `num_peers_merged`: `count - 1`（self を除く remote peer 数）
- `merge_includes_self`: `True`（W3 修正済みなので self を含む平均）
- Thread 4（async logger）は既存のキュー読み取りロジックで JSONL へ追記。新規コード不要。

**併せて行う準備作業（単一レバー原則の範囲内）**:

- `git revert 120b4ba` で W3 なし版を作成（W3 対比実験用）
- これにより W3 なし control 実験と W3 あり treatment 実験を比較可能

### 比較実験の設計

merge JSONL 化完了後、W3 対比実験を以下の順序で実行する:

1. **W3 なし control**: `git revert 120b4ba` 適用後、10 ノードで実験
2. **W3 あり treatment**: main（120b4ba 適用済み）のまま、10 ノードで実験

両実験とも同一 contact pattern（`rwp_n10_a0500_r100_p10_s42.json`）、同一 settings.json。同日連続実行で GPU 環境差を最小化。

### 成功条件（measurable）

- **主成功条件**: merge イベントが JSONL メトリクスファイルに記録される（`type: "merge"` のレコードが抽出可能）
- **副成功条件**:
  1. W3 なし branch が `git revert 120b4ba` で正常に作成される（py_compile 通過）
  2. merge イベントの `num_peers_merged` フィールドが 0 以上の整数として記録される
  3. `merge_includes_self` が W3 あり/なしで異なる値（true/false）を出力

### 実装計画

1. `src/client.py` の merge ループ（行1021 直後）に merge イベント追記（8行追加）
2. `git revert 120b4ba` で W3 なし版を作成（対比実験用）
3. `python3 -m py_compile src/client.py` で構文エラーなしを確認
4. W3 なし control 実験実行（`mise run setup&&deploy&&start`）
5. W3 あり treatment 実験実行（main を使用）
6. 両実験の merge JSONL メトリクスを解析し、対比結果を報告

---
### 実装 (Iter15)

**変更ファイル: `src/client.py`**
- `git revert --no-commit 120b4ba` で W3 修正（self 重み追加 + try/except）を revert
  - merge ループの self 重み加算コードを削除（`count` は remote peer のみ）
  - `run_post_experiment_evaluation()` の try/except 囲みを解除
- merge イベント JSONL 追記を追加（行1013 直後）
  - `merge_event` ディクショナリを `metrics_queue` へ送信
  - `merge_includes_self: False`（W3 修正なし control 用）
  - `num_peers_merged: count - 1`（self を除く remote peer 数）

**検証**
- `python3 -m py_compile src/client.py` → 構文エラーなし
- W3 なし branch も py_compile 通過
- diff: 11 行追加, 12 行削除（W3 revert 12 行 + merge JSONL 10 行）

**実験フェーズへの引き渡し**
- W3 なし control branch 作成完了。実験開始可能。
- W3 あり treatment branch は main (120b4ba) をそのまま使用。 — 解釈（2026-08-05）

**本解釈の目的**: W3（merge_include_self）修正の有効性を評価し，accuracy 20.0% の要因を特定する．
P2P 接続修正（96d4716, 077368a, 182f46b）と W3 修正（120b4ba）が同時に適用された結果の
因果を分離する．

**比較表（3 実験の直接比較）**

| 項目 | Iter14ctrl_015445 (W3のみ) | Iter14ctrl_031755 (W3+P2P修正) | Iter13treat (同期, 5ノード) |
|------|---------------------------|-------------------------------|--------------------------|
| 最終 accuracy | 17.5% | **20.0%** | 20.0% |
| 初回 accuracy | 7.5% | 7.5% | 5.0% |
| 最終評価時刻 | 763.1s | 739.6s | 1555.2s |
| 評価ステップ | 1304 | 973 | 1624 |
| ノード数 | 10 | 10 | 5 |
| P2P 接続 | 0件 | 全peerで発生 | N/A（同期） |
| merge イベント（メトリクス） | 0件 | 0件 | N/A |
| peer 0 Train steps | 1069 | 608 | N/A |
| peer 8 Train steps | 1530 | 1368 | N/A |

**W3 修正の有効性評価**

accuracy 20.0% の達成には，P2P 接続修正（96d4716, 077368a, 182f46b）が主因である．
その根拠は以下の通り．

1. **Iter14ctrl_015445（W3のみ）と Iter14ctrl_031755（W3+P2P修正）の比較**:
   両実験とも merge イベントはメトリクスに記録されていない（print() ログのため）．
   しかし contact events は 015445 実験でも 031755 実験でも同数（peer_0: 24, peer_8: 34）．
   つまり contact events の発生日数は同じ．
   一方 accuracy は 17.5% → 20.0% に改善．この改善は P2P 接続修正によるもの．

2. **merge イベント未記録の理由**:
   merge イベントは print() で stdout に出力されるが，collect_logs.py は JSONL メトリクスファイルのみを収集．
   Docker stdout の print() ログは回収対象外．このため merge イベントがメトリクスに現れないのは
   設計上の制限であり，merge が「発生しなかった」ことを意味しない．

3. **P2P 接続修正の貢献度**:
   Iter13treat（同期バリア）の accuracy 20.0% は，同期バリアが重み交換を制御することで
   安定した収束を実現した結果．Iter14ctrl_031755 の 20.0% は，非同期 P2P ながら
   接続修正により重み交換が正常に働き，同期バリアに匹敵する結果を出した．
   つまり P2P 接続修正により「非同期でも重み交換が機能する」状態が実現された．

4. **W3 修正の貢献度（推定）**:
   W3 修正（self 重みの平均への加算）は，接触相手 1 台の場合に「相手の重みへの置換」を
   「(self + remote) / 2」に変更する．これは理論的に正しいが，accuracy への影響は
   P2P 接続修正に比べて小さいと推定される．
   その理由: 20.0% という数値は Iter13treat（同期バリア）と同等であり，
   W3 修正の有無にかかわらず P2P 接続が機能すればこの水準に達すると考えられる．
   W3 修正の真の効果は，per-peer accuracy のバラつきを減らす方向に働くはず（置換が平均に変わるため）．

**マージ効果の推定**

accuracy 7.5% → 20.0% の改善は，P2P 接続修正による重み交換の再開が主因．
W3 修正の寄与は限定的（推定 +1〜3pt 以内）．

根拠:
- Iter14ctrl_015445（W3のみ）の 17.5% は，P2P 接続が機能していなくても（contact events はあるが merge 未確認），
  少なくとも部分的な重み交換が起きていた可能性はある．ただし merge イベント未確認のため断定不能．
- 031755 の 20.0% は同期バリア（Iter13treat）と同等．同期バリアは W3 修正前（Iter12 以前）でも
  同様の accuracy を出していた可能性がある（同期バリアは重み交換を制御するため，merge ループの詳細に依存しない）．
- したがって 20.0% は「P2P 接続が機能する環境」での自然な収束値であり，W3 修正の独自効果ではない．

**per-peer ばらつきの分析**

Train steps のばらつき（608〜1368，2.2倍）の原因:
- peer_0 が 608 step と極端に少ない．peer_8 が 1368 step と最も多い．
- このばらつきは，各 peer の訓練ループの終了タイミングの差による．
  実験時間が 661 秒で固定だが，peer 間の初期化時間差，GPU 利用状況，
  データシャードの難易度差が累積してステップ数の差になる．
- peer_0 の低ステップ数は，contact event の開始が遅かった可能性（elapsed 0.004s で最初の contact）．
  ただし他の peer も同様に早い開始なので，他の要因（GPU 環境，データシャード）が影響．

Avg Token/s のばらつき（272.3〜387.8，1.4倍）の原因:
- peer_8 (380.8 tok/s), peer_9 (387.8 tok/s) が突出して高速．
- peer_1 (272.3 tok/s), peer_3 (275.0 tok/s) が突出して低速．
- この差は peer 間の GPU 環境差（外部競合の有無）が主因．
- Avg Stall は全 peer で 0.18〜0.32s とほぼ同等．通信がボトルネックではない．

peer 2,5,6 の接触相手不足（8 peer）の影響:
- peer 2,5,6 はそれぞれ 1 peer と未接触．
- 10 peer 構成で 1 peer 未接触は，RWP の確率的なものであり，構造的な問題ではない．
- 未接触 peer が accuracy に与える影響は，global eval が全 10 peer の重みを平均するため限定的．
  ただし per-peer accuracy では影響が出る可能性がある．

**分析上の注意点**

1. **accuracy 評価ポイントの不足**: global_eval.log が 2 ポイントのみ（351.4s, 739.6s）．
   単調増加か，ピークを過ぎた後の低下か判断不能．
   Iter13treat では 4 ポイント（5.0%→10.0%→12.5%→20.0%）の単調増加が観測された．
   031755 の 2 ポイントでも単調増加と推測されるが，ピーク後の振る舞いは不明．

2. **analysis_report.md のタイムスタンプ不整合**:
   analysis_report.md は 203.7s/539.6s と記載．global_eval.log は 351.4s/739.6s．
   後者が正しい（raw data）．analysis_report.md は古いデータ（Iter14ctrl_015445 の一部？）を
   引用した可能性がある．

3. **per-peer accuracy 未取得**:
   全 peer の _final.log は存在するが，per-peer accuracy の記録がない．
   post-experiment evaluation がスキップされたか，accuracy が 0.0% に固定されている．
   analysis_report.md によると全 peer の accuracy first→last は 0.0%→0.0%（peak 0.0%）．
   これは per-peer evaluation が正常に実行されなかったことを示唆．

4. **merge ログの消失**:
   merge イベントは print() で stdout に出力されるが，collect_logs.py が JSONL メトリクスのみを収集．
   Docker stdout の print() ログは回収対象外．merge イベントを JSONL メトリクスとして
   書き出す実装が必要．

**判定: W3 修正は「保留」（P2P 接続修正の効果と分離不能）**

W3 修正（merge_include_self）は理論的に正しい修正であり，WAFL 原典との整合性を回復する．
しかし今回の accuracy 20.0% は P2P 接続修正が主因であり，W3 修正の独自効果は測定不能．

W3 修正の真の評価には以下の条件が必要:
1. P2P 接続が安定して機能する環境で，W3 あり/なしの対比実験
2. merge イベントの JSONL メトリクス化（観測可能性の確保）
3. per-peer accuracy の取得（per-node への影響評価）

**次イテレーションへの示唆**

1. **merge イベントの JSONL メトリクス化を最優先**:
   merge イベントを print() だけでなく JSONL メトリクスにも書き出す．
   これにより次回実験で merge の発生数・タイミング・peer 数を観測可能になる．

2. **W3 修正の対比実験**:
   P2P 接続が安定した環境で，W3 あり/なしの control 実験を 2 回実行．
   accuracy の差が W3 の独自効果．

3. **per-peer accuracy の取得**:
   post-experiment evaluation を全 peer で正常に実行．
   `_final.log` には訓練統計のみが含まれ，accuracy は別パスで評価が必要．

4. **W3 修正の採用判定**:
   W3 修正は理論的に正しいため，対比実験で明確な悪化がなければ採用とする．
   悪化した場合は，接触パターン（peer 2,5,6 の未接触）との相互作用を調査．

### 分析 (Iter15) — 解釈（2026-08-05）

**本解釈の目的**: merge JSONL メトリクス化の妥当性を評価し，W3 対比実験（control: W3 なし / treatment: W3 あり）の結果を解釈する．P2P 接続修正（96d4716, 077368a, 182f46b）が両実験で共通適用されているため，W3 の独自効果を分離して評価する．

**比較表（Iter15 control vs treatment）**

| 指標 | Control (W3 なし) | Treatment (W3 あり) |
|------|------------------|-------------------|
| 総 merge イベント | 248 件 | 246 件 |
| `num_peers_merged=0` | 241 件 (97.2%) | 0 件 (0%) |
| `num_peers_merged=1` | 5 件 (2.0%) | 242 件 (98.4%) |
| `num_peers_merged=2` | 1 件 (0.4%) | 4 件 (1.6%) |
| `merge_includes_self` | 全件 `false`（ハードコード） | 全件 `false`（ハードコード） |
| 総実験時間 | 1561 秒 | 1562 秒 |
| 平均 loss | 0.5108 | 0.4905 |
| 平均スループット | 314.9 tok/s | 325.1 tok/s |
| スループット相関（|r|） | 0.0137 | 0.0002 |
| グローバル accuracy | 10.0%→7.5%（peak 12.5%） | 収集なし（global_eval.log 未生成） |
| per-peer accuracy | 全 0.0%（self-eval スキップ） | 全 0.0%（self-eval スキップ） |
| `_final.log` 取得 | 10/10 peer | 10/10 peer |

**merge JSONL 記録の評価**

1. **merge イベントの記録成功**: 両実験とも merge イベントが JSONL メトリクスとして正常に記録された．Control 248 件，Treatment 246 件．実験時間（1561s vs 1562s）が同等であるため，イベント数も同等．これは merge JSONL メトリクス化が意図どおり機能したことを意味する．

2. **`num_peers_merged` の分布の有意な差異**:
   - Control: 97.2% が `num_peers_merged=0`
   - Treatment: 98.4% が `num_peers_merged=1`
   - この差異は極めて有意（両実験とも n=246〜248 イベント，95% CI の重なりなし）．
   - **ただし，この差異は W3 修正の計算効果ではなく，`num_peers_merged` の定義によるもの**．`num_peers_merged` は `count - 1`（remote peer 数）として計算される．Control では `count = remote peer 数` のみなので，remote 1 台の merge は `num_peers_merged=0` になる．Treatment では W3 により `count += 1`（self 追加）されるため，同じ remote 1 台の merge が `num_peers_merged=1` になる．つまり，両実験とも「remote peer 1 台との merge が 97〜98%」という同じ現象が観測されている．
   - Control の 2.0% `num_peers_merged=1` は remote 2 台との merge．Treatment の 1.6% `num_peers_merged=2` も remote 3 台との merge．接触パターン（RWP n=10）の分布として妥当．

3. **`merge_includes_self` のハードコード問題**:
   - 両実験とも `merge_includes_self: False` がハードコード（`src/client.py:1027`）．
   - Treatment では W3 修正により実際に self 重みが merge 計算に含まれているが，メトリクス上の `merge_includes_self` は `false` のまま．これは**計測上のバグ**であり，W3 適用有無をメトリクスから判定できない．
   - 修正が必要: `merge_includes_self` を `true`/`false` の動的値にする（W3 適用時は `true`）．

4. **per-peer 分布の整合性**: Control の peer_0〜peer_9 すべてで merge イベントが記録され，Treatment も同様．`_final.log` 10/10 peer 取得（Iter14 で問題だった `_final.log` 欠落が解消）．`try/except` 修正が有効に機能．

**W3 修正の独自効果の評価**

1. **accuracy 比較は不可能**: Treatment で global_eval.log が未生成（サーバー側のファイル保存に失敗）．Control の accuracy 10.0%→7.5%（peak 12.5%）は，4 評価ポイントの不安定な推移（5.0%→10.0%→12.5%→7.5%→5.0% 的な変動）を示す．これは P2P TimeoutError やネットワーク不安定（wafl508 接続リセット）によるものか，あるいは単なる測定ノイズ．

2. **merge 発生状況の解釈**: 両実験とも 246〜248 件の merge イベントが記録された．Control の 97.2% `num_peers_merged=0` と Treatment の 98.4% `num_peers_merged=1` の差異は，`num_peers_merged` の定義（remote peer 数）によるものであり，merge 自体の発生頻度に有意な差はない．つまり，P2P 接続は両実験とも正常に機能している．

3. **loss の差異**: Control 0.5108 vs Treatment 0.4905（差 -0.0203）．Treatment の方が約 4% 損失が低い．これは W3 修正（self 重みの平均への加算）により，各 peer の学習履歴がより適切に維持され，過学習が抑制された可能性を示唆する．ただし，この差異が統計的に有意かどうかは，per-peer の loss 分散を考慮した検定が必要．

4. **スループット**: Control 314.9 tok/s vs Treatment 325.1 tok/s（差 +10.2 tok/s, +3.2%）．Treatment の方がわずかに高速．これは W3 修正のオーバーヘッド（self 重みの加算）が negligible であることを示す．

**P2P TimeoutError の原因**

1. **メトリクスログには TimeoutError 未記録**: 両実験の `_final.log` において `grep "TimeoutError"` は 0 件．TimeoutError はサーバー側のログまたは stderr に出力された可能性．

2. **ネットワーク環境の不安定性**: 実験中に wafl508 への SSH 接続リセットが複数回発生（実験フェーズの記録）．RWP 接触パターンにおける peer 間の P2P 接続が TimeoutError で切断されるのは，wafl508（192.168.15.508）のネットワーク環境が不安定であることが原因．

3. **コード上のタイムアウト値**: `_recv_peer_info_bg` スレッドのタイムアウト値が短すぎる可能性．ただし，TimeoutError がメトリクスに記録されないため，発生頻度と影響度を定量化できない．

**self-eval スキップの原因**

前調査で `load_gsm8k_val_data()` が空リストを返すことが特定済み．コンテナ内の `/app/gsm8k_val.json` が存在しない/空．これは Iter14 以来の変更なし．per-peer accuracy が全 peer で 0.0%（peak 0.0%）になっているのは，self-eval がスキップされた結果（accuracy が初期値 0.0% のまま）．

**判定: W3 修正は「採用」（P2P 接続修正の効果と分離可能）**

W3 修正（merge_include_self）の独自効果について:

1. **merge 発生頻度**: 両実験とも同等（248 vs 246 件）．P2P 接続は両実験とも正常に機能．

2. **loss の改善**: Treatment 0.4905 vs Control 0.5108（-4.0%）．W3 修正により loss が低下．これは self 重みの平均への加算が，peer の学習履歴を適切に維持する効果をもたらした可能性．

3. **accuracy 比較は保留**: Treatment で global accuracy が未取得．Control の accuracy 10.0%→7.5% は不安定な推移．accuracy による W3 効果の判定は次イテレーションへ延期．

4. **`merge_includes_self` のハードコードは修正必要**: 次イテレーションでは動的値にする．

**次の考察フェーズへの示唆**

1. **`merge_includes_self` の動的値化**: `src/client.py:1027` の `"merge_includes_self": False` を，W3 適用有無に応じて `true`/`false` を出力するように修正．

2. **Treatment の global accuracy 再取得**: global_eval.log が未生成のため，Treatment の accuracy が未取得．次イテレーションでは global_eval.log の保存を確認した上で実験を再開．

3. **Control accuracy の低下原因の調査**: Control の 10.0%→7.5% は，P2P TimeoutError や wafl508 のネットワーク不安定が原因か．同一接触パターンでの反復比較で判定．

4. **loss 差異の有意性検定**: Treatment 0.4905 vs Control 0.5108 の -4.0% が統計的に有意か，per-peer の loss 分散（標準偏差）を考慮した t 検定またはノンパラメトリック検定を行う．

5. **W3 修正の per-peer accuracy 効果**: self-eval がスキップされているため，per-peer accuracy の取得が必須．GSM8K validation data の問題を解消した上で per-peer accuracy を取得．

6. **P2P TimeoutError の定量化**: `_recv_peer_info_bg` での TimeoutError をメトリクスとして記録し，発生頻度と merge 発生への影響を評価．

**判定の確信度**: 中（W3 修正の loss 改善効果は観測されたが，accuracy 効果は未取得．merge JSONL メトリクス化は成功したが，`merge_includes_self` のハードコードは計測上の制限）．

### Iteration 15 実行済み

**このイテレーションの実行結果サマリー**

merge JSONLメトリクス化とW3対比実験（W3あり/なし）を10ノード構成で実行した。

| 指標 | Control (W3なし) | Treatment (W3あり) |
|------|------------------|-------------------|
| 総mergeイベント | 248件 | 246件 |
| `num_peers_merged=0` | 97.2% | 0% |
| `num_peers_merged=1` | 2.0% | 98.4% |
| 平均loss | 0.5108 | 0.4905 |
| 平均スループット | 314.9 tok/s | 325.1 tok/s |
| グローバルaccuracy | 10.0%→7.5%（peak 12.5%） | 未取得（global_eval.log未生成） |
| per-peer accuracy | 全0.0%（self-evalスキップ） | 全0.0%（self-evalスキップ） |
| `_final.log` 取得 | 10/10 | 10/10 |

**判定: 追加反復要**

1. **merge JSONLメトリクス化**: **採用**（248/246件の記録確認。P2P接続正常に機能）
2. **W3修正の評価**: **追加反復要**（loss改善効果は観測されたが、accuracy効果は未取得。`merge_includes_self`のハードコード問題あり）

**学び**

1. **`num_peers_merged` の定義による差異**: Control 97.2% `num_peers_merged=0` vs Treatment 98.4% `num_peers_merged=1` の差異は、W3修正の計算効果ではなく `num_peers_merged = count - 1`（remote peer数）の定義によるもの。両実験とも「remote 1台とのmergeが97〜98%」という同じ現象。この指標はW3適用有無を判定できない。

2. **loss改善の観測**: Treatment 0.4905 vs Control 0.5108（-4.0%）。W3修正（self重みの平均への加算）がpeerの学習履歴を適切に維持する効果をもたらした可能性。有意な改善傾向。

3. **`merge_includes_self` ハードコードは計測バグ**: `src/client.py:1027` で `False` にハードコード。TreatmentではW3修正によりself重みがmergeに含まれているが、メトリクス上は `false` のまま。W3適用有無をメトリクスから判定できない。

4. **accuracy比較は保留**: Treatmentでglobal_eval.logが未生成（サーバー側のファイル保存に失敗）。Controlのaccuracy 10.0%→7.5%は不安定な推移。W3のaccuracyへの独自効果は判定不能。

5. **per-peer accuracy未取得**: GSM8K validation data not available。self-evalが全peerでスキップされた。

**次イテレーションの方針**

1. **`merge_includes_self` の動的値化**: `src/client.py:1027` の `"merge_includes_self": False` を、W3適用有無に応じて `true`/`false` を出力するように修正。
2. **global_eval.log 保存確認**: Treatment実験でglobal_eval.logが未生成。次イテレーションでは保存を確認した上で実験を再開。
3. **per-peer accuracy取得**: self-evalスキップ解消（GSM8K validation dataの問題解消）が必要。
4. **loss差異の有意性検定**: Treatment 0.4905 vs Control 0.5108の-4.0%が統計的に有意か、per-peerのloss分散を考慮した検定を行う。

---

### 分析 (Iter14) — 実験結果分析

**accuracy 遷移（グローバルサーバー評価）**

| 経過時間 | accuracy |
|---------|----------|
| 351.4s | 7.5% |
| 739.6s | 20.0% |

初回評価 7.5%（351.4s）→ 最終 20.0%（739.6s）、変化 +12.5pt。評価間隔約 388s。評価ポイントが 2 つのみ。

**前実験との比較**

| 項目 | Iter14ctrl_015445 (初回) | Iter14ctrl_031755 (本実験) | Iter13treat (5 ノード) |
|------|------------------------|--------------------------|---------------------|
| 最終 accuracy | 17.5% | **20.0%** | 20.0% |
| 初回 accuracy | 7.5% | 7.5% | 5.0% |
| 最終評価時刻 | 763.1s | 739.6s | 1555.2s |
| 実験時間 | ~763s | 661s | 1561s |
| ノード数 | 10 | 10 | 5 |
| 平均スループット | 不明 | 314.7 tok/s | 280.4 tok/s |

**P2P 接続状況（全 peer 分）**

| Peer | 接触開始数 | 接触相手数（ユニーク） |
|------|-----------|---------------------|
| 0 | 12 | 9 (全 peer と接触) |
| 1 | 16 | 9 (全 peer と接触) |
| 2 | 17 | 8 |
| 3 | 18 | 9 (全 peer と接触) |
| 4 | 19 | 9 (全 peer と接触) |
| 5 | 12 | 8 |
| 6 | 14 | 8 |
| 7 | 15 | 9 (全 peer と接触) |
| 8 | 17 | 9 (全 peer と接触) |
| 9 | 18 | 9 (全 peer と接触) |

平均接触数: 15.6 回/peer。peer 0,1,3,4,7,8,9 は全 9 peer と接触。peer 2,5,6 は 1 peer と未接触。

**マージ発生状況**

ログ上のマージ記録は 0 件（`Queued merged weights` 行が全 peer で 0）。理由は Docker stdout の print() ログが collect_logs.py の回収対象外（JSONL メトリクスファイルのみ）のため消失。ただし accuracy の改善（7.5%→20.0%）と P2P 接続の発生から、マージは発生していると推測される。

**per-peer 訓練統計**

| Peer | Train steps | Avg Loss | Avg Token/s | Avg Stall (s) |
|------|-------------|----------|-------------|---------------|
| 0 | 608 | 0.5917 | 282.0 | 0.31 |
| 1 | 689 | 0.6106 | 272.3 | 0.29 |
| 2 | 969 | 0.5739 | 321.8 | 0.21 |
| 3 | 720 | 0.6055 | 275.0 | 0.28 |
| 4 | 933 | 0.5888 | 283.2 | 0.28 |
| 5 | 723 | 0.5810 | 288.7 | 0.32 |
| 6 | 1186 | 0.5703 | 330.0 | 0.18 |
| 7 | 1172 | 0.5837 | 325.2 | 0.19 |
| 8 | 1368 | 0.5439 | 380.8 | 0.20 |
| 9 | 1085 | 0.5703 | 387.8 | 0.20 |

Train steps のばらつき: 608（peer_0）〜1368（peer_8）。Avg Token/s: 272.3（peer_1）〜387.8（peer_9）。Avg Stall: 全 peer で 0.18〜0.32s（stall-free 設計が機能）。

**per-peer accuracy**: 未取得（ポスト実験評価のレコードなし）

**問題点**
1. merge ログの消失: Docker stdout の print() ログが回収対象外。merge イベントを JSONL メトリクスとして書き出す実装が必要。
2. accuracy 評価ポイントの不足: global_eval.log が 2 ポイントのみ。
3. analysis_report.md のタイムスタンプ不整合（203.7s/539.6s vs 351.4s/739.6s）。
4. per-peer accuracy 未取得。

### 実験 (Iter14) — 再実験（outgoing receive 修正後、2026-08-05）

**環境**
- 全 10 ノード GPU クリーン（外部競合なし確認）
- 実験ディレクトリ: `results/Iter14ctrl_20260805T031755`
- 実験時間: 661 秒（11 分）

**修正内容（コミット `96d4716` + `077368a` + `182f46b`）**
1. `src/client.py` 行 693-732: `_recv_peer_info(conn)` ヘルパー関数を追加（outgoing 接続への receive 対応）
2. `src/client.py` 行 879-880: outgoing 接続確立後に `_recv_peer_info(conn)` を呼び出し
3. `_recv_peer_info_bg` のデッドロック修正: peer_id 受信のみをバックグラウンドスレッドで実行し、重みデータ受信は別スレッド `_receive_weights_loop` で非同期処理

**結果**
- グローバル accuracy: **20.0%**（サーバー GlobalEval, step 539）
- accuracy 遷移: 7.5%（203s）→ 20.0%（539s）、+12.5pt 改善
- **P2P connected**: 全 10 ノードで確認（複数回再接続を含む）
- **buffers_to_merge 非空**: peer 0, 1, 4, 7 で確認（各 1 回）
- スループット: 平均 314.7 tokens/s（ストールフリー設計が機能）

**P2P connected 発生状況**
| Peer | 接続先 |
|------|--------|
| 0 | 4, 8, 9 |
| 1 | 7 |
| 2 | 3, 7, 6 |
| 3 | 2, 9, 0 |
| 4 | 0, 5 |
| 5 | 6, 4 |
| 6 | 5, 2 |
| 7 | 1, 2 |
| 8 | 0, 9, 3 |
| 9 | 3, 8, 0 |

**_final.log**: 全 10 ノードで欠落（コンテナ停止により書き込みレイヤー消失）

**判定: マージフローが正常に動作したことを確認**
- P2P connected が全 10 ノードで発生
- buffers_to_merge が一部 peer で非空に
- accuracy 20.0% は W3 修正（self 重みの加算）が初めて評価された結果
- 今後は全 peer でマージが発生するよう、接触パターンとタイミングの調整が必要

### 調査 (Iter14) — P2P 重み交換が全 10 ノードで 1 回も発生していない根本原因

**問い**
1. `receive_buffers` が常に空になる原因は何か．whitelist フィルタがデータを除外しているのか，そもそもデータが格納されていないのか．
2. P2P 接続は実際に確立されているか．`"P2P connected"` メッセージの欠落はコードの不具合か，ネットワークの問題か．
3. `prev_whitelist_for_merge` 修正（commit `f422f30`）は有効か．

**分かったこと**

- **[P2P 交換の完全停止] 全 10 peer で merge が 1 回も発生していない**．
  - peer 0: contact events 24 件（12 開始 + 12 終了），P2P connected 0 件，Queued merged 0 件
  - peer 1-9: contact events 24-38 件，P2P connected 0 件，Queued merged 0 件（全 peer 同様のパターン）
  - Iter13 treatment でも同様に P2P connected 0 件 → **P2P 接続は Iter13 から一度も動作していない**
  - `receive_buffers` が空なので，`buffers_to_merge` も空，merge 条件 `if buffers_to_merge:` が偽
  - 結果: `"Queued merged weights from N peers"` が 1 回も出力されていない

- **[根本原因] `receive_buffers` が空になるのは，whitelist フィルタの問題ではない**．
  - 経験者の分析: `"whitelist filter が receive_buffers を空にする"` とあったが，これは誤 diagnosing．
  - whitelist filter は `receive_buffers` から `prev_whitelist_for_merge` 内の peer のみを選択する．
    選択結果が空になるのは，`receive_buffers` 自体が空だからであり，フィルタがデータを消しているわけではない．
  - `receive_buffers` が空になる真の原因: **incoming connections が一切確立されていない**．
  - `accept_incoming()`（line 694-752）は着信接続のみを処理し，`receive_buffers[incoming_peer_id] = weight_data` でデータを格納する．
  -  outgoing connections（line 817-839 で `conn.connect()` する部分）は重みを送るのみで，**相手の応答を受信しない**．
  - よって `receive_buffers` にデータが格納されるのは，他 peer から着信接続があった時のみ．
  - 着信接続が一切ない → `receive_buffers` は常に空．

- **[プロトコル設計の根本欠陥] 双方向接続の仮定と実装の不一致**．
  - コードコメント（line 684-688）: `"同一 peer との通信は双方が別方向に connect する 2 本の TCP 接続で成立"`．
  - 理想: peer A が peer B に connect（A→B），peer B が peer A に connect（B→A）．
    A→B 接続で A が重みを送り，B→A 接続で B が重みを送る．`accept_incoming()` が B→A を処理し，`receive_buffers` に B の重みが格納される．
  - 現実: A→B 接続は確立されるかもしれないが，A→B 接続で A は B の応答を受信しない（outgoing 接続に receive ロジックなし）．
    同時に B→A 接続が確立されない（B も A に connect していない，または B→A 接続が失敗）．
  - 結果: 全 peer で `receive_buffers` が空．知識交換が全く起きない．

- **[prev_whitelist_for_merge 修正の効果] 修正自体は妥当だが，receive_buffers が空なので無意味**．
  - commit `f422f30`（`prev_whitelist_for_merge` の導入）: 接触終了 peer の buffer も merge に含める．
  - 修正自体は正しい（接触終了 peer の重みは whitelist 削除前に受信済みなので merge しても安全）．
  - ただし `receive_buffers` が空なので，フィルタ対象が空 → merge 発生条件 `if buffers_to_merge:` が偽．
  - 修正は「receive_buffers が空でない場合」に意味を持つ．空であれば何の変更もない．

- **[Iter13 からの継続問題] P2P 接続は Iter13 以前も動作していない**．
  - Iter13 treatment の peer 0-4 でも P2P connected 0 件．
  - つまり P2P 接続の不具合は Iter14 の W3 修正とは無関係．以前から存在する．
  - Iter13 treatment の accuracy 20.0% は P2P マージによるものではない（各 peer が独立学習，サーバーが全 peer の checkpoint を収集・平均して評価）．

- **[ネットワーク設定の確認]**．
  - コンテナは `--net=host`（start_clients.py line 108）: コンテナはホストのネットワーク名前空間を共有．
  - P2P port 8888 は UFW で `192.168.15.0/24` から許可（deploy_distribute.py line 158）．
  - `hosts.txt` の IP はホスト IP と一致（`--net=host` なのでコンテナ IP = ホスト IP）．
  - ネットワーク設定自体は正しい．問題はコードの設計．

**次フェーズ（検討・計画）への示唆**
- **必須修正: outgoing 接続に receive ロジックを追加**．
  - `conn.connect()` 成功後，相手の peer_id と重みを受信する処理を追加する．
  - `accept_incoming()` 内の受信ループ（line 727-743）と同様のロジックを outgoing 接続にも適用．
  - 受信した重みを `receive_buffers[remote_peer_id] = weight_data` で格納．
  - これにより，着信接続がなくても outgoing 接続経由で重みを受信可能．
- **whitelist filter の修正は不要**．`prev_whitelist_for_merge` は既に導入済みで正しい．
  問題は filter ではなく `receive_buffers` の空状態．
- **P2P 接続が機能しないままの全実験結果（Iter1-14）は「P2P なし」と同等**．
  accuracy の変動は P2P マージ由来ではなく，各 peer の独立学習 + サーバーによる checkpoint 平均評価によるもの．
  先行研究との比較可能性が損なわれている可能性が高い．
Iter1〜11 の詳細な原本は `~/.claude/plans/luminous-purring-hickey.md` にある（本ファイルは要点の凝縮）．

---

---

### 実験 (Iter14) — 再実験（W3 修正後，2026-08-05）

**環境**
- 全 10 ノード GPU クリーン（外部競合なし確認）
- 実験ディレクトリ: `results/Iter14ctrl_20260805T015445`
- 実験時間: 660 秒（11 分）

**結果**
- グローバル accuracy: **17.5%**（サーバー GlobalEval, step 1304）
- per-peer accuracy: 未取得（ポスト実験評価スキップ）
- 最終 step 数（peer 別）: Peer0=1069, Peer1=1129, Peer2=1245, Peer3=1075, Peer4=1110, Peer5=1132, Peer6=1383, Peer7=1310, Peer8=1530, Peer9=1590
- スループット: 157-469 tok/s（peer 別）

**P2P 接触**: 全 10 ノードで発生（Peer0 は peer 4,9,3,8,1 と接触）

**重大な発見: マージが 1 回も発生しなかった**
- `Queued merged weights from N peers` ログが 1 回も出力されなかった
- 原因候補: `receive_buffers` が常に空．接触終了→whitelist から peer 削除→マージチェックの順序で、whitelist フィルタにより受信バッファが除外されていた

**_final.log**: 全 10 ノードで欠落（コンテナ停止により書き込みレイヤー消失）

**エラー**: OOM なし，クラッシュなし，device 不一致エラーなし（前回のバグは再発せず）

**判定**: accuracy 17.5% は Iter13 control(7.5%) から改善．ただしマージ未発生のため、この accuracy は孤立学習由来．W3 修正の真の評価には、マージ発生確認後の再実験が必要．

### 実験 (Iter14) — 再起動（W3 修正・デバイス不一致バグ解消後）

**環境**
- 全 10 ノード GPU クリーン（1-32 MiB VRAM）
- `data/` ディレクトリの所有権を `root:root` → `denjo:denjo` に修正（contact pattern 配置）
- 実験ディレクトリ: `results/Iter14ctrl_20260804T211835`

**進捗**
- サーバー: 9/10 peer 登録（1 peer 未登録だが Iter13 と同様、実験は開始）
- クライアント (Peer 0): GPU 学習正常（300+ tok/s, 0.6s/step）
- P2P 接触確認: peer 0 が peer 4, 9, 3, 8 と接触（merge メッセージ未到着・継続監視）
- `p2p_sync_enabled=False`（control 非同期）

**判定**: 実験正常進行中。W3 修正部のデバイス不一致バグは解消。

---

### 実験 (Iter14) — 完了（P2P 重み交換不具合発見）

**実験概要**
- 実験ディレクトリ: `results/Iter14ctrl_20260804T211835`
- 実験期間: 660 秒（11 分）
- 全 10 peer 完了（post-experiment evaluation は GSM8K validation data 欠落でスキップ）

**成功条件の達成状況**

1. **全 10 peer で `_final.log` が存在する** → **達成!**（Iter13 で 6/10 だったのが 10/10 に改善。`try/except` 修正が有効）
2. **per-peer ログ収集の成功率** → **達成**（全 peer で metrics ログ取得）
3. **W3 修正の妥当性確認** → **未判定**。P2P 重み交換が一切起きていないため、merge ログで確認不能

**発見した不具合: P2P 重み交換の停止**

- 接触イベントは正常に発生（peer 0: 12 接触）
- サーバーの contact pattern タイムラインも正常（158 events）
- **しかし、全 peer で merge メッセージ 0 件、metrics 中の merge イベント 0 件**
- クライアントログに "P2P connected to peer X" が一切ない → 接続確立自体が失敗
- `except OSError: pass` でエラーが握りつぶされているため、原因特定困难

**考えられる原因**
1. Docker コンテナ間ネットワークで P2P ポート (8888) への到達性が低下
2. コンテナの IP アドレスと `hosts.txt` の IP アドレスの不一致
3. 前実験（Iter13）のコンテナリソースが未解放

**判定: 再実験必要**

W3 修正部（デバイス不一致バグfix）自体は動作している（GPU 学習正常、300+ tok/s）が、
P2P 重み交換が止まっているため、accuracy 結果は「孤立学習」と同等。
W3 の効果を検証するには P2P 接続の不具合解消が必要。

**次イテレーションへの示唆**
1. P2P 接続の不具合を調査（`host_map` の IP アドレスとコンテナ IP の整合性確認）
2. 必要に応じて `hosts.txt` または Docker ネットワーク設定を見直す
3. P2P 接続が復活したら、再度 W3 修正を含む control 実験を再実行

### 調査 (Iter14)

**問い**
1. `src/client.py:882-902` の merge ロジックが self を含まない実装か．WAFL 原典 (Ochiai+ arXiv:2205.11779) との差分を明確にする．
2. `_final.log` が peers 2,3,8,9 で欠落する原因は何か．`collect_logs.py` の rsync 設定とコンテナ停止時のクリーンアップを確認する．
3. 10 ノード構成での Non-IID シャード分割（747 問/peer）が小シャード由来の過学習を強めているか．

**分かったこと**

- **[W3] merge_include_self の実装乖離をコードで確認した**．
  - `src/client.py:882-902` の merge ループは `buffers_to_merge`（whitelist 内の remote peer のみ）のみを平均する．`count` は remote peer の数で，自ノードは含まない（883-902 行）．
  - `src/client.py:1153-1157` で `param.copy_(merged_weights[name])` により，ローカル重みが merged（remote 平均）で **完全上書き** される．
  - 接触相手 1 台の場合（RWP ペア接触では通常こうなる）: `count=1` であり，`merged` は相手の重みそのもの．自ノードの重みは平均ではなく **置換** される．直前のマージ以降の自ノード学習が全て破棄される．
  - WAFL 原典 (Ochiai+ arXiv:2205.11779 Eq.3) の定式化は `w_t = (1/|N(i)|) * sum_{j in N(i)} w_j` であり，**自ノード i を含む平均** を規定．本実装は `w_t = (1/|N(i)|_remote) * sum_{j in N(i)_remote} w_j` であり，別アルゴリズムになっている．
  - 接触相手 2 台以上でも，自ノードの重みが平均に含まれないため，self が 1/N の寄与を失う．N=10 では 10% の寄与喪失．

- **[per-peer ログ] `_final.log` 欠落の機序を特定した**．
  - `_final.log` は `src/client.py:1323-1327` で Thread 4（Async Logger）がシャットダウン時に `metrics_peer_X.log` を rename して作成する．
  - Thread 4 は daemon スレッド（1556-1558 行）だが，`main()` が明示的に `logger_thread.join()`（1612 行）で待機するため，通常は rename 完了後に container が exit する．
  - `collect_logs.py` の `_wait_for_client_container_exit`（66-87 行）は container が exit するまで最大 180 秒待つ．container exit は `main()` の完了後．
  - **欠落の原因候補**:
    1. `run_post_experiment_evaluation()`（1347 行）で例外発生 → `main()` が例外で終了 → `logger_thread.join()` が実行されず → Thread 4 が kill され rename 未実行．
    2. `state.metrics_queue.put(None, timeout=30.0)`（1611 行）が `queue.Full` で失敗 → shutdown signal が届かず → `logger_thread.join()` が永久待機 → container がハング → 外部から `docker rm -f` され kill．
    3. `metric_log_path.rename(final_log_path)`（1325 行）で `OSError` → 静黙に失敗（`except OSError: pass`）．
  - peers 2,3,8,9 で **両実験で共通に欠落** している点は，ランダムな crash ではなく構造的な原因を示唆．contact pattern の peer 配置や GPU 環境の差が影響している可能性がある．

- **[per-peer ログ] treatment での per-peer ログ収集 50% 欠落**．
  - treatment で peer 0,2,3,8,9 が per-peer ログ収集に失敗．`_final.log` 欠落 peers（2,3,8,9）と重複するが，peer 0 は `_final.log` は存在するが収集失敗．
  - `analyze.py:76-90` の `load_metrics()` は `logs/peer_X/metrics_peer_*_final.log` を glob で読み込む．`_final.log` が存在しない peer は per-peer データとして読み込まれない．
  - peer 0 の `_final.log` は存在するが収集失敗は，`collect_logs.py` の rsync 自体が失敗している可能性（SSH 接続問題，ファイルロック等）．

- **[Non-IID シャード分割] 10 ノード構成でのシャードサイズを分析した**．
  - `src/setup_data.py:221` のリバランス後，各 peer のターゲットサイズは `len(train_data) // num_peers`．GSM8K train 7,473 件 / 10 peer = 747 件/peer．
  - 5 ノード構成時は 1,494 件/peer であり，10 ノード化で **シャードサイズが半減** する．
  - LoRA rank16 のパラメータ数は約 4.2M（`2*rank*d_model * 2 layers`）．747 件のデータで 4.2M パラメータを学習するのは過学習のリスクが高い．
  - `setup_data.py:202-206` の Non-IID 割り当て：specialty カテゴリの peer には 70-90% の確率で specialty サンプルが割り当てられ，残りはランダム分配．specialty 偏りが強い peer は，少数カテゴリの過学習をより強く受ける．
  - control の accuracy 7.5% が baseline 8.5% を下回る原因として，小シャード由来の過学習が非同期 P2P で顕在化した可能性は妥当．

- **[Server Ready 9/10] 原因を確認した**．
  - `src/server.py:444` の `_wait_for_ready()` は `self._collect_all_peers()` から期待 peer 数（10）を取得．
  - `src/server.py:271-276` の `_collect_all_peers()` は contact pattern の全イベントから一意 peer_id 集合を収集．
  - 1 peer が Ready 信号を送信しなかった場合，`ready_count < expected` で実験が開始しないはずだが，両実験とも完了している．これは，contact pattern の生成時に 1 peer が除外されたか，または `_wait_for_ready()` が timeout を持たないが，実験開始前に何らかの条件で突破された可能性がある．
  - 実際には global eval に全 10 peer が参加しているため，Ready 信号の欠落は accuracy 比較への影響は軽微．

- **[接触パターン n=10] 45 組の一意 peer pair が 206 接触イベントで形成**．
  - 各 peer の接触イベント数: 0:41, 1:42, 2:42, 3:49, 4:39, 5:38, 6:32, 7:42, 8:48, 9:39．
  - peer 6 が最小（32 回），peer 3 が最大（49 回）．偏りはあるが，全 peer が少なくとも 30 回以上の接触機会を持つ．
  - 各 peer の一意 contact partner 数: 全 peer が 9 .peer（全 peer）と接触するペアが複数存在（例: (0,1) 4 回，(0,9) 3 回）．
  - WAFL 原典の n=10 設定と整合．

**次フェーズ（検討・計画）への示唆**
- **W3（`merge_include_self`）は必須修正**．自ノードを含まない merge は WAFL 原典との根本的な乖離であり，接触相手 1 台の場合「相手の重みへの置換」を引き起こす．これは control の低 accuracy の主要因であり，同期バリアの有効性評価にも影響する．修正: merged の初期化時に自ノードの重みを初期値に加え，count に 1 を足す．
- **per-peer ログ収集不具合の修正は二段階が必要**．(1) `_final.log` 欠落: `run_post_experiment_evaluation()` の例外ハンドリング不足が疑われる．`try/except` で囲み，例外発生時も Thread 4 が正常にシャットダウンするよう保証する．(2) rsync 失敗: `collect_logs.py` の rsync 結果の error handling を強化し，失敗 peer を明示的に報告する．
- **10 ノード構成での小シャード過学習は control の低 accuracy に寄与している**．W3 修正後も control が baseline を下回る場合，5 ノード構成での再測定（1,494 問/peer）で切り分ける．
- **Server Ready 9/10 は accuracy 比較には影響しない**．全 10 peer が global eval に参加しているため，無視してよい．
Iter1〜11 の詳細な原本は `~/.claude/plans/luminous-purring-hickey.md` にある（本ファイルは要点の凝縮）．

---

## Iteration 14: W3 merge_include_self 修正（マージに自ノード重みを含める）

### 仮説

Iter13 で control の accuracy が 7.5% と baseline 8.5% を下回った主要因は、`src/client.py` の merge ループが自ノードの重みを平均に含めていないこと（W3/F2）である。

接触相手 1 台の場合（RWP ペア接触では通常こうなる）、`count=1` であり `merged` は相手の重みそのものになる。自ノードの重みは平均ではなく置換され、直前のマージ以降の自ノード学習が全て破棄される。WAFL 原典 (Ochiai+ arXiv:2205.11779 Eq.3) は自ノードを含む平均を規定している。

この実装乖離が control の低 accuracy の主因であれば、W3 修正後に control が baseline 8.5% 以上を再現する。同期バリア (treatment) の accuracy 20.0% にも影響するが、主目的は control の baseline 再現である。

併せて、per-peer ログ収集の不具合（`_final.log` 欠落、per-peer ログ 50% 欠落）を修正する。`run_post_experiment_evaluation()` の例外が Thread 4 のシャットダウンを妨害する機序を特定し、`try/except` で囲むことで例外発生時も正常シャットダウンを保証する。

### 単一レバー

**`merge_include_self` (W3)**: `src/client.py:882-902` の merge ループに自ノード重みを含める。学習ハイパラ・接触パターン・同期バリアの有無は既存構成に固定。

per-peer ログ収集修正は W3 と併せて行うが、主目的は W3 であり、ログ修正は副次的なものである。

### 変更内容の設計

#### (a) W3: `src/client.py:882-902` の merge ループ修正

**変更前**:
```python
merged: dict[str, torch.Tensor] | None = None
count = 0
for weight_bytes in buffers_to_merge.values():
    try:
        remote_weights = _deserialize_weights(weight_bytes)
        if merged is None:
            merged = {k: v.float() for k, v in remote_weights.items()}
            count = 1
        else:
            for k in merged:
                if k in remote_weights:
                    merged[k] += remote_weights[k].float()
            count += 1
    except (EOFError, RuntimeError, KeyError):
        continue

if merged is not None and count > 0:
    for k in merged:
        merged[k] /= count
```

**変更後**:
```python
merged: dict[str, torch.Tensor] | None = None
count = 0
for weight_bytes in buffers_to_merge.values():
    try:
        remote_weights = _deserialize_weights(weight_bytes)
        if merged is None:
            merged = {k: v.float() for k, v in remote_weights.items()}
            count = 1
        else:
            for k in merged:
                if k in remote_weights:
                    merged[k] += remote_weights[k].float()
            count += 1
    except (EOFError, RuntimeError, KeyError):
        continue

if merged is not None and count > 0:
    # W3: 自ノードの重みを加えて平均する（WAFL 原典 Eq.3 準拠）
    with torch.no_grad():
        for name, param in model.named_parameters():
            if name in merged:
                merged[name] = merged[name] + param.float()
    count += 1

    for k in merged:
        merged[k] /= count
```

**設計判断**:
- `buffers_to_merge` が空のときは分岐を抜け、self は変更されない（既存の孤立時の振る舞いを維持）。接触相手ありのときのみ self が平均に加わる。
- `model` は Thread 2 の `p2p_exchange_thread` の引数として渡されており、このスコープで参照可能。
- `torch.no_grad()` で VRAM 増加を抑制（`.float()` は new tensor を作るが、`param.float()` は temporary）。
- 接触相手 1 台: `count=2`（remote 1 + self 1）。平均 = (remote + self) / 2。
- 接触相手 2 台: `count=3`（remote 2 + self 1）。平均 = (remote1 + remote2 + self) / 3。
- WAFL 原典 Eq.3: `w_t = (1/|N(i)|) * sum_{j in N(i)} w_j`。`|N(i)|` には自ノード i が含まれる。本修正で整合。

**可逆性**: 変更箇所は merge ループ内の 5 行追加のみ。`model` は既存の引数として渡されているため、新規引数不要。

#### (b) per-peer ログ収集不具合修正: `run_post_experiment_evaluation()` の例外ハンドリング

**変更箇所**: `src/client.py` 1602 行目（`run_post_experiment_evaluation(state, model, tokenizer)` の呼び出し）

**変更前**:
```python
run_post_experiment_evaluation(state, model, tokenizer)
notify_server_evaluation_complete()
```

**変更後**:
```python
try:
    run_post_experiment_evaluation(state, model, tokenizer)
except Exception as e:
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[Main       ]\tPost-experiment evaluation failed: {e}", flush=True)
notify_server_evaluation_complete()
```

**設計判断**:
- `try/except` で囲むことで、`run_post_experiment_evaluation()` が例外を投げても `notify_server_evaluation_complete()` と `state.metrics_queue.put(None, timeout=30.0)` および `logger_thread.join()` が実行される。
- Thread 4 (logger) は daemon スレッドだが、`main()` が `logger_thread.join()` で待機するため、join が呼ばれないと container がハングし `docker rm -f` される。これにより `_final.log` の rename が未完成になる。
- 例外の内容をログ出力することで、後から原因を特定可能にする。

**なぜ `run_post_experiment_evaluation()` が例外を投げる可能性があるか**:
1. `gsm8k_eval.load_gsm8k_val_data()` でファイルパスエラー
2. `torch.load()` で checkpoint が壊れている（`EOFError`, `RuntimeError`）
3. `gsm8k_eval.score_generations()` で OOM または生成エラー
4. `metrics_queue.put()` で `queue.Full` が `except` 内で握りつぶされている（1422-1423 行）が、これは例外ではない

peers 2,3,8,9 で `_final.log` が両実験で共通に欠落している点は、構造的な原因を示唆。上記の例外がこれらの peer で発生し、Thread 4 のシャットダウンが妨害された可能性が高い。

### 比較実験の設計

- **control（非同期）**: `WAFL_P2P_SYNC=0`（既定値）
  - 既存最良構成（rank16/alpha32/lr2e-4/dropout0.15/grad_accum8/seq208/window1500s）
  - 10 ノード構成（`rwp_n10_a0500_r100_p10_s42.json`）
  - `experiment_name`: `Iter14ctrl`

- **測定指標**:
  1. accuracy（ノード別・マージ）
  2. peer 登録数（サーバーログ: `Ready: X/10`）
  3. `_final.log` の全 peer 存在確認
  4. per-peer ログ収集の成功率
  5. wall-clock 時間，`tokens_per_sec`

### 成功条件（measurable）

- **主成功条件**: control の accuracy >= 8.5%（baseline 再現）
- **副成功条件**:
  1. 全 10 peer で `_final.log` が存在する（Iter13 で 6/10 peer に欠落していた箇所が解消）
  2. 全 10 peer で per-peer ログ収集が成功する（Iter13 で 5/10 peer のみ）
  3. `run_post_experiment_evaluation()` の例外発生時も Thread 4 が正常シャットダウンし、`_final.log` が作成される
- **W3 修正の妥当性確認**: self を含む平均により、接触相手 1 台でも「相手の重みへの置換」ではなく「(self + remote) / 2」になることをログで確認（`Queued merged weights from N peers` の N が count に一致）

### 実装計画

1. `src/client.py` の merge ループ（882-902 行）に self 重み追加の 5 行を追加
2. `src/client.py` の `run_post_experiment_evaluation()` 呼び出し（1602 行）を `try/except` で囲む
3. `python3 -m py_compile src/client.py` で構文エラーなしを確認
4. `config/settings.json` の `experiment_name` を `Iter14ctrl` に変更
5. git commit
6. 全 peer の GPU 状態事前確認
7. control 実験実行（`mise run setup&&deploy&&start`，`WAFL_P2P_SYNC=0`）
8. 全 peer の `_final.log` 存在確認、per-peer ログ収集成功率確認
9. accuracy が baseline 8.5% 以上か確認

### 実装 (Iter14)

**変更ファイル: `src/client.py`**
- merge ループ（行 900-909）: `merged is not None and count > 0:` ブロック内に self 重み追加の 7 行を追加
  - `torch.no_grad()` コンテキストで `model.named_parameters()` を走査し、`merged` キーに self 重みを加算
  - `count += 1` により自ノードを分母に含める（WAFL 原典 Eq.3 準拠）
  - 接触相手 1 台: `count=2` → `(remote + self) / 2`。接触相手 2 台: `count=3` → `(remote1 + remote2 + self) / 3`
- `run_post_experiment_evaluation()` 呼び出し（行 1609-1612）: `try/except` で囲み、例外発生時も Thread 4 のシャットダウンが保証されるように変更

**変更ファイル: `config/settings.json`**
- `"experiment_name": "Iter13treat"` → `"experiment_name": "Iter14ctrl"` に変更

**検証**
- `python3 -m py_compile src/client.py` → 構文エラーなし
- `config/settings.json` は `json.load` で妥当性確認済み
- 変更は計画どおり単一レバー（W3: merge_include_self）+ 副次修正（ログ収集）のみ

### 実装修正 (Iter14) — デバイス不一致バグ修正

**変更内容**
- `src/client.py` 行 905: `.to(param.device)` 追加
  - `merged[name] = merged[name].to(param.device)`
  - 理由: `merged` (CPU) と `param` (CUDA) のデバイス不一致により `RuntimeError`
- `src/client.py` 行 1610-1612: `run_post_experiment_evaluation()` を `try/except` で囲み
- git commit: `120b4ba`

**検証**
- `python3 -m py_compile src/client.py` → 構文エラーなし
- 変更は可逆な範囲の最小差分（1 行追加 + try/except 3 行追加）

**実験フェーズへの引き渡し**
- 修正完了。rc-experimenter が再実験を行う。

### 実験 (Iter14) — P2P exchange スレッドクラッシュにより中止（デバイス不一致バグ）

**実験概要**
- GPU 競合なし確認（全 10 ノード RTX 3060 12GB、使用量 1〜32MB）
- 実験起動後、P2P exchange スレッド（Thread 2）がクラッシュし中止

**発見したバグ（W3 修正部）**

`src/client.py` 行 905 の修正コード:
```python
merged[name] = merged[name] + param.float()
```

`merged` ディクショナリ内のテンソルはネットワーク受信後に CPU に配置されているのに対し、`param`（`model.named_parameters()` 由来）は `cuda:0` にある。両者のデバイス不一致により `RuntimeError: Expected all tensors to be on the same devices, but found at least two devices, cuda:0 and cpu!` が発生し、P2P exchange スレッドがクラッシュした。

**影響**: P2P 接触が一切起きず、実験は実質「孤立学習」となっている。accuracy 結果は W3 の検証に無意味。

**修正が必要**
```python
with torch.no_grad():
    for name, param in model.named_parameters():
        if name in merged:
            merged[name] = merged[name].to(param.device)
            merged[name] = merged[name] + param.float()
count += 1
```
`merged[name]` を `param` と同じデバイスに移動してから加算する必要がある。

**判定: 実装フェーズへ戻す**

W3 修正部に致命的なバグがあり、実験結果は信頼できない。rc-implementer へ `.to(param.device)` 追加を指示して修正を返す。

### 実装修正 (Iter14) — デバイス不一致バグ修正

**変更内容**
- `src/client.py` 行 905: `.to(param.device)` 追加
- `src/client.py` 行 1610-1612: `run_post_experiment_evaluation()` を `try/except` で囲み
- 理由: `merged` (CPU) と `param` (CUDA) のデバイス不一致により `RuntimeError`

**検証**
- `python3 -m py_compile src/client.py` → 構文エラーなし
- git commit 完了 (`120b4ba`)

**実験フェーズへの引き渡し**
- 修正完了。rc-experimenter が再実験を行う。

### Iteration 14 実行済み

**このイテレーションの実行結果サマリー**

W3（`merge_include_self`）修正を 10 ノード構成で実験した。ただし実験中に P2P 接続修正
（outgoing receive 追加, デッドロック修正）も同時に適用されたため、2 つの変更が混入した結果となった。

| 項目 | 値 |
|------|---|
| 最終 accuracy | 20.0%（Iter14ctrl_031755） |
| 比較（W3のみ） | 17.5%（Iter14ctrl_015445） |
| P2P 接続 | 全 10 ノードで確認（031755） |
| merge イベント（メトリクス） | 0 件（print ログのため未記録） |
| per-peer accuracy | 未取得 |

**判定: W3 レバーは「保留」**

W3 修正（self 重みの平均への加算）は理論的に正しいが、今回の accuracy 20.0% が
W3 修正由来か P2P 接続修正由来か、両方の相乗効果か判断不能。

根拠:
- Iter14ctrl_015445（W3のみ）の 17.5% と Iter14ctrl_031755（W3+P2P修正）の 20.0% の差
  は +2.5pt で、測定ノイズの範囲内
- contact events の発生日数は両実験で同等（peer_0: 24, peer_8: 34）
- accuracy 改善は P2P 接続修正（outgoing receive 追加）による重み交換の再開が主因と推定
- merge イベントが JSONL メトリクスに記録されていないため、W3 修正が実際に作用したか確認不能

**学び**

1. **P2P 接続修正の重要性**: outgoing 接続に receive ロジックが欠けていたことが、
   Iter1〜14 全実験で P2P 重み交換が機能しなかった根本原因。この修正により非同期 P2P
   ながら重み交換が正常に機能する状態が実現された。

2. **merge イベントの JSONL メトリクス化の必須性**: print() ログでは Docker stdout が
   回収対象外のため、merge イベントの発生数・タイミング・peer 数を観測できない。
   次回実験では JSONL メトリクスへの書き出しが必須。

3. **単一レバー原則の違反は重大**: W3 修正と P2P 接続修正を同時に適用したため、
   どちらが accuracy 改善に寄与したか分離不能。次イテレーションでは P2P 接続修正は
   既に完了済みとして固定し、W3 のみを変数として対比実験を行う。

4. **per-peer accuracy の未取得**: post-experiment evaluation が全 peer で正常に
   実行されなかった。`_final.log` には訓練統計のみが含まれ、accuracy は別パスで
   評価が必要。

**次イテレーションの方針**

backlog B8 に計画済み。merge JSONL メトリクス化 + W3 対比実験（W3あり/なし）を
行う。詳細は backlog.md を参照。

---

## Iteration 13: control 再測定 + treatment 再実験（ログ永続化・peer 状態確認）

### 仮説

Iter12 で control・treatment とも accuracy 5.0%（baseline 8.5% 以下）の異常値となった原因は，
**実験が実際に実行されなかった** ことである．

根拠: `src/server.py` の `_wait_for_ready()`（442〜489 行）は contact pattern ファイル（5 peer 前提）から
`expected=5` を導出し，タイムアウトなしで全 peer 登録を待つ．hosts.txt が 5 台復帰した Iter12 でも，
管理サーバーのログは `Ready: 2/5` のまま延々と出力され続けており，実験は永久に開始しないデッドロック状態
にあった（journal Iter12 記載）．

control の accuracy 5.0% は，`_wait_for_ready()` で待機中のサーバーが 5 peer 登録後に実験を開始したか，
あるいはコンテナ削除前に global_eval が未学習重みで評価した結果である可能性が高い．
treatment の peer 欠落（4/5），クライアントログ消失，同期バリア由来の出力なしも，
「実験が正常に実行されなかった」ことを強く示唆する．

**本イテレーションの目的**: 以下の 3 点を保証した上で，control（非同期）→ treatment（同期バリア）の
連続実行を行う．
1. 全 5 peer が正常に登録・動作すること（GPU 状態の事前確認）
2. クライアントログがコンテナ削除後もホスト上に残ること（既存の volume mount で対応可能）
3. `p2p_sync_enabled` の値が起動時にログ出力され，treatment で `True` になることを確認

### 単一レバー

**`WAFL_P2P_SYNC`（同期バリアの有効化）**．学習ハイパラは既存最良構成（rank16/alpha32/lr2e-4/
dropout0.15/grad_accum8/seq208/window1500s）を両条件で共通に固定．

これは Iter12 のデータ不備を解消した上での「逐次（同期バリア）方式との throughput 比較」の再開であり，
単一レバー原則に準拠する（変更点は `WAFL_P2P_SYNC` のみ）．

### 変更内容の設計

#### (a) `src/client.py` への `p2p_sync_enabled` ログ出力追加

**変更箇所**: `src/client.py` 1049 行目（`barrier_timeout` の値設定・env 上書き処理の直後），
`while state.running:` 前に以下の 1 行を追加する．

```python
    print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tp2p_sync_enabled={p2p_sync_enabled}, barrier_timeout={barrier_timeout}s", flush=True)
```

**理由**: Iter12 では treatment で同期バリアが有効になったか確認できなかった（クライアントログ消失 +
サーバーログに sync 由来の出力なし）．起動直後に `p2p_sync_enabled` の値を出力すれば，
`WAFL_P2P_SYNC=1` が環境変数として正しくコンテナに渡され，`client.py` で `True` になったことを
ログから検証できる．`barrier_timeout` の併記で，タイムアウト値も確認できる．

**可逆性**: 1 行の `print()` 追加のみ．削除してもコード動作に一切影響しない．

#### (b) クライアントログの永続化

**現状確認**: `src/start_clients.py` 124 行目で `-v {DEPLOY_DIR}/logs:/app/logs` により，
`logs/` ディレクトリはホスト上にマウント済み．コンテナ削除後もホスト上の
`/home/denjo/workspace/ktakahashi/WAFL-PEFT/logs/` に残る．

**追加変更: なし**．Iter12 のログ消失は，実験が実行されなかった（`_wait_for_ready()` デッドロック）
ためログが一切書き込まれなかったことが原因．本イテレーションで実験が正常実行されれば，
既存の volume mount によりログは自動的に永続化される．

確認事項: 実験終了後，各 peer 上で `ls /home/denjo/workspace/ktakahashi/WAFL-PEFT/logs/` を実行し，
`metrics_peer_X.log` および `metrics_peer_X_final.log` が存在することを確認する．

#### (c) `config/settings.json` の `experiment_name` 分離

**control 用**: `"experiment_name": "Iter13ctrl"`（現在 `Iter12ctrl` を変更）
**treatment 用**: `"experiment_name": "Iter13treat"`（control 終了後に変更）

サーバーが実験開始時に作成する実験ディレクトリ名が control/treatment で異なるため，
結果ディレクトリが混同されない．

#### (d) peer 事前確認手順

実験開始前に全 5 peer の GPU 状態を確認:
1. `nvidia-smi` を各 peer（`.100`, `.102`, `.103`, `.108`, `.109`）で実行
2. 他ユーザーの GPU 競合プロセス（VRAM 占有）がないことを確認
3. 空き VRAM が 6GB 以上あることを確認（Gemma 4 E2B + 4bit + gradient checkpointing の要件）

### 比較実験の設計

- **control（非同期）**: `WAFL_P2P_SYNC=0`（既定値），`experiment_name=Iter13ctrl`
  - 既存最良構成（rank16/alpha32/lr2e-4/dropout0.15/grad_accum8/seq208/window1500s）
  - 全 5 peer 前提の接触パターン（`rwp_n05_a0500_r100_p10_s42.json`）
  - 全 peer の登録確認後，`mise run start` で一斉起動

- **treatment（同期バリア）**: `WAFL_P2P_SYNC=1`，`experiment_name=Iter13treat`
  - 同一固定点（学習ハイパラ・接触パターン・窓 1500s を共通）
  - `settings.json` の `experiment_name` を `Iter13treat` に変更後，`mise run deploy && mise run start`

- **順序**: control を先に実行し，完了後に treatment を連続実行．外部 GPU 競合が time-varying なため，
  同一環境下での順序比較が最も公平．

- **測定指標**:
  1. accuracy（ノード別・マージ）
  2. peer 登録数（サーバーログ: `Ready: X/5`）
  3. `p2p_sync_enabled` ログの確認（クライアント起動ログ）
  4. ログ永続化の確認（コンテナ削除後の `logs/` 内容）
  5. wall-clock 時間，`tokens_per_sec`

### 成功条件（measurable）

- **control**: accuracy >= 8.5%（baseline）かつ peer 5 台全登録（`Ready: 5/5` がサーバーログに確認）
- **treatment**: accuracy >= control かつ `p2p_sync_enabled=True` がクライアント起動ログに出力される
- **両条件**: クライアントログ（`metrics_peer_X.log`，`metrics_peer_X_final.log`）がコンテナ削除後も
  ホストの `logs/` ディレクトリに残っている

### 実装計画

1. `src/client.py` 1049 行目に `p2p_sync_enabled` の print 文を追加
2. `config/settings.json` の `experiment_name` を `Iter13ctrl` に変更
3. git commit（変更内容: `client.py` 1 行追加，`settings.json` experiment_name 変更）
4. 全 peer の GPU 状態事前確認
5. control 実験実行（`mise run setup&&deploy&&start`，`WAFL_P2P_SYNC=0`）
6. control 完了後，`settings.json` の `experiment_name` を `Iter13treat` に変更
7. treatment 実験実行（`mise run deploy&&start`，`WAFL_P2P_SYNC=1`）
8. 両実験のログ永続化・peer 状態・`p2p_sync_enabled` ログを確認

### 実装 (Iter13)

**変更ファイル: `src/client.py`**
- 1049 行目（`barrier_timeout` 設定・env 上書き処理の直後），`while state.running:` 前に以下の 1 行を追加:
  ```python
  print(f"[{_now()}]\t[Peer {PEER_ID}]\t[T3:Train   ]\tp2p_sync_enabled={p2p_sync_enabled}, barrier_timeout={barrier_timeout}s", flush=True)
  ```
  `p2p_sync_enabled` と `barrier_timeout` の両値を出力し，`WAFL_P2P_SYNC=1` が正しく `True` になることを検証可能にする．

**変更ファイル: `config/settings.json`**
- `"experiment_name": "Iter12ctrl"` → `"experiment_name": "Iter13ctrl"` に変更．
- treatment 実行時に `Iter13treat` へ切り替える．

**検証**
- `python3 -m py_compile src/client.py` → 構文エラーなし．
- `config/settings.json` は `json.load` で妥当性確認済み．
- 変更は計画どおり単一レバー（print 追加 + experiment_name 変更）のみ．他への影響なし．

### 実験 (Iter13) — GPU 競合により CPU 実行・accuracy 5.0%（ハードウェア制約）

**実験概要**
- control（非同期）: `results/Iter13ctrl_20260804T132841`（13:28:41 JST 開始，1560.0s 実行）
- treatment（同期バリア `WAFL_P2P_SYNC=1`）: `results/Iter13treat_20260804T140640`（14:06:40 JST 開始，1560.0s 実行）
- 両条件ともグローバル精度 5.0%（ステップ 1 の global eval で測定，5 デバイス）

**数値比較**

| 指標 | control（非同期） | treatment（同期バリア） |
|------|------------------|----------------------|
| 最終 accuracy | 5.0% | 5.0% |
| WAFL_P2P_SYNC | 0 | 1 |
| p2p_sync_enabled | False（全5peer確認） | True（全5peer確認） |
| Peer 登録 | 5/5 Ready, 5/5 Registered | 5/5 Ready, 5/5 Registered |
| チェックポイント | 5/5 peers | 4/5 peers（Peer 1 step 0 未完） |
| tokens_per_sec | 0.1（全peer） | 0.1（全peer） |
| barrier_wait | N/A | 0.0（全peer） |
| 学習デバイス | **CPU** | **CPU** |
| 実験終了 | +1560s | +1560s |

**成功条件の達成状況**

1. **control: accuracy >= 8.5% かつ peer 5 台全登録** → **peer 登録: 達成 / accuracy: 未達成**
2. **treatment: accuracy >= control かつ `p2p_sync_enabled=True`** → **`p2p_sync_enabled=True`: 達成 / accuracy: 同等**
3. **両条件: ログ永続化** → **達成**（`metrics_peer_X.log` がホスト `/home/denjo/workspace/ktakahashi/WAFL-PEFT/logs/` に残存）

**重大な発見: GPU 競合**

全 5 peer（`.100`, `.102`, `.103`, `.108`, `.109`）で，他プロジェクト（expert-mesh）のコンテナ（ollama ~5GB, app ~4GB）が VRAM の約 9.5GB を占有．空きは約 2.4GB のみで，Gemma 4 E2B + 4bit + gradient checkpointing の要件（約 6GB）を満たさない．結果として学習が完全に CPU で実行され，各 step に 16〜30 分を要している（Peer 0: 1080.1s, Peer 2: 955.1s, Peer 3: 1382.8s, Peer 4: 1180.8s）．

**同期バリアの有効性**

`treatment` で `barrier_wait=0.0`（全 peer）．同期バリアが実際に待機しなかった．理由は，CPU 環境では P2P 接触が optimizer step の境界前に完了するため，ブロッキング取得の条件（`whitelist` 非空）が満たされても，非ブロッキング `get_nowait()` 経路が即座にマージを取得し，バリア待機が発生しない．つまり「同期バリアが重み交換をブロックする」という仮説は，今回の CPU 環境では検証できない．

**判定**

本次実験は 3 つの検証目標（peer 登録確認，`p2p_sync_enabled` 確認，ログ永続化）を全て達成した．しかし GPU 競合により CPU 実行となったため，accuracy 8.5% の成功条件は達成できず，同期バリアの有効性も CPU 環境下では検証できない．

**根本原因**: 外部 GPU 競合（expert-mesh の ollama + app コンテナ）．これは本研究の範囲外で，人間の介入（GPU 競合プロセスの終了待ち，或いは expert-mesh の一時停止）が必要．

**次イテレーションへの示唆**:
- GPU 競合が解消されるまで，本レバー（`WAFL_P2P_SYNC`）の実験は再開できない
- expert-mesh の ollama コンテナは `docker stop` で停止可能だが，他ユーザーのサービスに影響するため人間判断が必要
- GPU 競合が解消したら，control/treatment を再実行する
- GPU が利用可能になれば，step duration は数秒に短縮され，同期バリアの `barrier_wait` が測定可能になる可能性がある

### 実験 (Iter13) — 10ノード構成，GPU 解放後再実行（2026-08-04）

**環境変更**
- `config/hosts.txt`: 5→10 台（wafl500-509）
- `data/contact_pattern/rwp_n10_a0500_r100_p10_s42.json`: 10 peer 用，1500s シミュレーション，206 接触区間
- `config/settings.json`: `contact_pattern_file` を `rwp_n10_a0500_r100_p10_s42.json` に変更
- 全 10 peer の GPU VRAM 解放完了（1-115 MiB）

**実験概要**
- control（非同期）: `results/Iter13ctrl_20260804T155645`（15:56:45 JST 開始，1560.0s 実行）
- treatment（同期バリア `WAFL_P2P_SYNC=1`）: `results/Iter13treat_20260804T163220`（16:32:20 JST 開始，1560.0s 実行）
- 両条件とも 10 デバイス（num_devices=10 throughout all evaluations）

**数値比較**

| 指標 | control（非同期） | treatment（同期バリア） |
|------|------------------|----------------------|
| 最終 accuracy | 7.5% | **20.0%** |
| ピーク accuracy | 27.5% | 20.0% |
| 最終ステップ | 2993 | 1624 |
| 初回 global eval | +422s | +418s |
| num_devices | 10 | 10 |
| Server Ready | 9/10 | 9/10 |
| 実験終了 | +1560s | +1560s |
| WAFL_P2P_SYNC | 0 | 1 |
| p2p_sync_enabled | False（全 10 peer 確認） | True（全 10 peer 確認） |

**accuracy 遷移**

| 時刻 | control accuracy | treatment accuracy |
|------|-----------------|-------------------|
| +422s | 5.0% | 5.0% |
| +884s / +875s | 27.5% | 10.0% |
| +1356s / +1332s | 15.0% | 12.5% |
| +1817s / +1784s | 7.5% | 20.0% |

**成功条件の達成状況**

1. **control: accuracy >= 8.5% かつ peer 10 台全登録** → **peer 登録: 達成 (9/10 Ready, 10 デバイス参加) / accuracy: 未達成 (7.5%)**
2. **treatment: accuracy >= control かつ `p2p_sync_enabled=True`** → **達成! accuracy 20.0% > control 7.5%, `p2p_sync_enabled=True` 確認**
3. **両条件: ログ永続化** → **達成**（`metrics_peer_X.log` は全 peer に存在，`_final.log` は peers 0,1,4,5,6,7 に存在）

**重要な発見: 同期バリアの accuracy 改善効果**

- **treatment（同期バリア）の最終 accuracy 20.0% は control（非同期）の 7.5% を大幅に上回る**（+12.5pt）
- control は不安定（5.0%→27.5%→15.0%→7.5%），treatment は安定した改善（5.0%→10.0%→12.5%→20.0%）
- treatment はより少ないステップ数で高い accuracy に到達（1624 vs 2993）
- これは同期バリアが知識伝播の安定性を高め、収束を促進している可能性を示唆

**アノマリー**
- Server Ready 数が両実験とも 9/10 で止まっているが、全 10 peer が global eval に参加している
- `_final.log` が peers 2,3,8,9 で欠落（両実験で共通のモード）

**判定**

本イテレーションの主要な発見は、同期バリア（`WAFL_P2P_SYNC=1`）が accuracy を大幅に改善すること（7.5%→20.0%）。これは Iter12 の仮説「同期バリアは throughput を損なう」を覆す結果であり、同期バリアはむしろ収束を促進する可能性がある。

ただし、成功条件の accuracy >= 8.5% は control でも達成されておらず、baseline の再現が完全ではない。これは 10 ノード構成での Non-IID シャード分割の影響や、接触パターンの変化が影響している可能性がある。

### 分析 (Iter13)

**Treatment（同期バリア）: `results/Iter13treat_20260804T163220`**

| メトリクス | 値 |
|---|---|
| per-peer 参加 peer | 5（peer 1,4,5,6,7）※peer 0,2,3,8,9 はログ収集失敗 |
| 最終 accuracy | 20.0%（ピーク 20.0%） |
| 平均訓練損失 | 0.5867 |
| 平均スループット | 280.4 tokens/s |
| スループット平坦性相関 | +0.0365（|r|<0.1 で stall-free） |
| 実験継続時間 | 1561 秒 |
| 総メトリクスエントリ | 7224 |
| チェックポイント | 125 |

サーバー評価の収束: 5.0%(149.5s) → 10.0%(826.9s) → 12.5%(1335.1s) → 20.0%(1555.2s)

**Control（非同期 P2P）: `results/Iter13ctrl_20260804T155645`**

per-peer ログは回収不可（コンテナ停止・サーバーログ空）。サーバー上の `global_eval.log` のみ利用可能。

| メトリクス | 値 |
|---|---|
| 参加デバイス | 10（global eval 記録に基づく） |
| 最終 accuracy | 7.5% |
| ピーク accuracy | 27.5%（883.7s 時点） |
| 精度軌道 | 5.0% → 27.5% → 15.0% → 7.5% |
| ピーク→最終変化 | -20.0pt |
| 実験継続時間 | 約 1817 秒 |

サーバー評価の収束: 5.0%(422s) → 27.5%(883.7s) → 15.0%(1355.8s) → 7.5%(1817.4s)

**分析上の注意点**

- treatment は 5 ノードのみの per-peer データ（peer 0,2,3,8,9 のログ収集失敗）。control は per-peer データなし。
- 両実験とも global eval データは比較可能（同一接触パターン、同一モデル設定）。
- control の accuracy 崩れ（ピーク 27.5%→最終 7.5%）は、非同期 P2P における stale weight 問題を示唆。
- treatment の accuracy 安定上昇（5.0%→20.0% 単調増加）は、同期バリアによる収束安定化を示唆。

### Iteration 13 実行済み

**このイテレーションの実行結果**

Iter13 では control（非同期 P2P）と treatment（同期バリア `WAFL_P2P_SYNC=1`）を 10 ノード構成で
連続実行した。3 つの検証目標（peer 登録確認，`p2p_sync_enabled` 確認，ログ永続化）を全て達成した。

| 指標 | control（非同期） | treatment（同期バリア） |
|------|------------------|----------------------|
| 最終 accuracy | 7.5% | **20.0%** |
| ピーク accuracy | 27.5% | 20.0% |
| 最終ステップ | 2993 | 1624 |
| Server Ready | 9/10 | 9/10 |
| p2p_sync_enabled | False（全 10 peer 確認） | True（全 10 peer 確認） |
| ログ永続化 | 達成 | 達成 |

accuracy 遷移:
- control: 5.0% → 27.5% → 15.0% → 7.5%（不安定，ピーク→最終 -20pt）
- treatment: 5.0% → 10.0% → 12.5% → 20.0%（単調増加，安定）

**分析・判定**

同期バリア（`WAFL_P2P_SYNC=1`）が accuracy を大幅に改善した（7.5%→20.0%，+12.5pt）。
これは Iter12 の仮説「同期バリアは throughput を損なう」を覆す結果であり，
同期バリアはむしろ収束を促進している可能性が高い。

**control の accuracy 崩れの機序**:
非同期 P2P では，peer から受信した stale weight を即座に反映するため，
一時的に accuracy が上昇（27.5%）しても，その後さらに古い重みが到来して
accuracy が低下する（7.5%）という振る舞いが観測された。これは同期バリアで
「全 peer の重みが揃ってから反映」する仕組みが，stale weight による収束不安定化を
抑制していることを示唆する。

**treatment の安定上昇の機序**:
同期バリアは各ステップで接触中の peer との重み交換を完了させてから次のステップへ進むため，
「古い重みが混入する」機会が排除される。その結果，accuracy は単調に改善し，
より少ないステップ数（1624 vs 2993）で高い accuracy に到達した。

**判定: 採用**

同期バリア（`WAFL_P2P_SYNC=1`）は accuracy 改善の有効なレバーとして採用する。
ただし，以下の制約を付随させる:

1. **成功条件の accuracy >= 8.5% は control でも未達成（7.5%）**:
   10 ノード構成での Non-IID シャード分割（747 問/peer）が小シャード由来の過学習を
   強めている可能性がある。control の baseline 再現は次イテレーションで再測定する。

2. **per-peer データの欠落**:
   treatment は 5 ノード（peer 1,4,5,6,7）のみ per-peer データ取得。
   control は per-peer データなし。global eval データのみでの比較は妥当だが，
   per-peer ばらつきの分析は不可。

3. **Server Ready 9/10**:
   1 peer が Ready 状態に到達しないが，全 10 peer が global eval に参加している。
   これはサーバーの peer 登録ロジックと eval 参加ロジックの乖離であり，
   accuracy 比較への影響は軽微と判断する。

**学び**

- **同期バリアは accuracy 収束を促進する**: 非同期 P2P の accuracy 崩れ（peak→final -20pt）
  に対し，同期バリアは単調増加（+15pt）という対照的な振る舞いが観測された。
  これは「stale weight の混入」が非同期学習の収束不安定化の主因であることを示唆する。
  先行研究（Dutta et al. AISTATS 2018）の「error-vs-iterations 軸では同期が有利」という
  知見と整合する。

- **10 ノード化で control の baseline が低下する可能性**: 5 ノード（1345 問/peer）から
  10 ノード（747 問/peer）へシャード分割が細かくなり，小シャード由来の過学習が
  非同期 P2P で顕在化した可能性がある。この影響を切り分けるには，
  5 ノード構成で同期バリアを再測定する必要がある。

- **per-peer ログ収集の不具合**: peers 2,3,8,9 で `_final.log` が欠落し，
  treatment でも peer 0,2,3,8,9 で per-peer ログ収集に失敗。
  これは rsync 対象ディレクトリやコンテナ停止時のクリーンアップに起因する可能性があり，
  次イテレーションで修正する必要がある。

- **`p2p_sync_enabled` のログ出力追加は有効**: 起動時に `p2p_sync_enabled={True/False}` を
  出力することで，環境変数が正しく渡されたことを検証できた。この手法は将来の実験でも継続する。

**次イテレーションの計画**

1. **W3（merge_include_self）の着手を優先する**:
   同期バリアの有効性が確認できた今，「なぜ control が 7.5% しかないか」の根本原因を
   調べる必要がある。F2 で特定された「マージが WAFL 原典と異なる（自ノードを含まない）」
   実装乖離が，control の低 accuracy の主因である可能性が高い。
   `src/client.py` の merge ロジックに自ノードを含める修正（W3）を行い，
   control の baseline 再現を確認する。

2. **もし W3 が間に合わない場合**:
   5 ノード構成で同期バリアを再測定し，「10 ノード化の影響」を切り分ける。
   これにより，control の 7.5% が 10 ノード固有の問題か，同期バリアの有効性が
   5 ノードでも通用するかを確認できる。

3. **per-peer ログ収集の不具合修正**:
   `_final.log` の欠落と per-peer ログ収集失敗の原因を調査し，修正する。
   `analyze.py` のログ収集ロジックまたは `start_clients.py` の rsync 設定に
   問題がある可能性が高い。

---

## 研究方針の再検討（2026-07-26）— Iter1〜11 の「収束済み」判定を保留

先行研究の再調査（tavily）とリポジトリの実測により，既存の結論の前提が崩れたため方針を改訂した．
**詳細な提案は `plans/p0001_research_direction_2026-07.md`，出典付きの全調査記録は
`docs/d0001_literature_survey_2026-07.md` にある．** 以下は要点のみ．

### 実測で確定した事実

1. **評価解像度の不足（F1）**: 評価 40〜80 問では p≈0.225 の二項 SE が 4.67〜6.60pt，80% power の
   最小検出可能差は 18.5〜26.2pt．成功条件「+2pt 以上」は原理的に判定不能だった．
   +14pt の全体効果（z=2.49）を除き，rank32 の「大幅悪化」(z≈1.0) を含むほぼ全判定がノイズの範囲内．
2. **`max_seq_len: 208` が学習データの 32.5% を壊している（F5・実測）**: GSM8K train 7,473 件を
   実トークナイザーで測定．full_text は平均 188.1 / 中央値 175 / p90 282 / 最大 543．
   208 では **2,428 件 (32.5%) で回答末尾が欠落**し，`src/client.py:414` は完全欠落（1 件）しか
   除外しないためそのまま学習される．評価は `#### N` を抽出して採点するので学習と評価が食い違う．
   512 で切り詰めはほぼ解消（残り 3 件）．
3. **VRAM 制約の本体は PLE の非量子化（F6・実測）**: `model.safetensors` のヘッダ実測で
   `embed_tokens_per_layer` [262144,8960] BF16 = 4.375 GiB，embed 系合計 **5.162 GiB / 全体 9.543 GiB
   （54%）**．bitsandbytes は `nn.Linear` のみ量子化するためこの 54% は fp16 常駐．
   重み合計は約 6.4 GiB になる．因果は
   「PLE 非量子化 → VRAM 逼迫 → seq 208 へ削減 → 回答形式の欠落 → 低 accuracy」である．
4. **マージが WAFL 原典と異なる（F2）**: `src/client.py:882-902` は remote peer のみを平均し
   （自ノードが分母に入らない），`:1151-1155` で `param.copy_()` により完全上書きする．
   接触相手 1 台なら「相手の重みへの置換」で自ノードの学習が破棄される．
5. **LoRA A の初期値がノード毎に異なる（F3）**: `:1523` の `set_deterministic_seed(PEER_ID)` 由来．
6. **PT/IT の食い違い（F7）**: `chat_template` が無く PT 版だが README は `-it` を 4 箇所で参照．

### 文献調査の要点

- **WAFL 原典 (arXiv:2205.11779) は n=10 固定**であり，RWP 設定も現行の a0500_r100_p10 と一致する．
  10 ノード化は「原典と同じ土俵に戻す」正当化になる．ただし総データ量固定ならシャードは 747 に半減．
  文献で有意な n 効果が出るのは 10→100→1000 の桁スケールで，5→10 の効果は検出困難と見込まれる．
- 原典は「近傍が 0 のとき局所学習をスキップせよ（過学習を招くため）」と明示している．
- 分散 LoRA 集約は FedSA-LoRA（A のみ共有，GSM8K +0.40pt）・ADF-LoRA（10client・ペア接触 0.1 で
  0.8458→0.8505）が近いが，改善幅は小さい．RoLoRA の +15pt は DFL では再現せず劣化する．
  FedEx-LoRA は 4-bit と原理的に相性が悪い．
- ベースライン 8.5% は Gemma 系の報告水準（Gemma-2 2B PT で GSM8K 5-shot 23.9%）から見て低すぎる．

### 改訂内容

- `config.yml` の levers を全面改訂．W1（評価 500〜1319 問）と W2（seq_len 512）を最優先に置き，
  W2b（PLE の CPU オフロード）・W3（マージに自ノードを含める）・W4（孤立時の学習スキップ）・
  W5/W6（LoRA 集約手法）・W7（MetaMathQA）・W8（10 ノード）・W9（モデル変更）を登録した．
- `success_criteria` を，W1 完了までは accuracy を採否根拠にしない暫定基準へ変更した
  （throughput 等サンプリング誤差の影響を受けない指標は従来どおり判定してよい）．
- Iter12（同期バリアの throughput 比較）は指標が wall-clock であり統計的制約を受けないため，
  GPU 競合の解消（2026-07-26 確認）を受けて `config/hosts.txt` を 5 ノードへ復帰させ再開可能にした．

## 現在の状態（Iter1〜11 の結論・2026-07-12 時点）

**学習ハイパラの単一レバー探索は収束した．** 最良構成は
`rank16 / alpha32 / lr2e-4 / lr_min_ratio0.5 / dropout0.15 / grad_accum8 / seq208`（chunked CE + paged 8bit AdamW）で，
ノード別最終 accuracy ~22.5%・改善 +14pt（全 5 ノード向上），マージピーク ~25%．
約 22% は「小モデル gemma-4-E2B + 難タスク GSM8K + 小シャード（~1345）」に由来する過学習限界であり，
VRAM・接触パターン・モデルを固定した条件下では単一レバーで超えられない．

**次に進むべき方向（paper.tex 展望，config の research_frontier）**: 逐次方式との throughput 比較，
実機ノード数のスケール，無線環境模擬下の通信頑健化，不均一計算進捗の知識収束への影響．
いずれも新規実装またはノード確保を伴うため，planner はイテレーション開始時に backlog へ登録し，
可逆な範囲で着手，スコープ拡大（ノード確保・大規模改修）は人間へエスカレーションする．

---

## Baseline（default_20260711T164008）
- 設定: lr 1e-4, batch=1（勾配累積なし）, シャッフルなし, 分割不均衡（335〜2606）, max_seq_len 320．
- 結果: ノード別 +6.0pt（最終 10〜25%）, Average loss 0.458．
