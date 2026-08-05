# docs/ — 設計・調査の索引

このディレクトリには，コードからは読み取れない知見（文献調査，実測記録，研究方針の根拠）を置く．
実装の使い方・アーキテクチャの説明は，リポジトリ直下の `README.md` にある．

**命名規約**: 複数ファイルにまたがる機能や横断的な調査は `d0000_TITLE.md` とし，連番は作成順に振る
（`plans/` 側は `p0000_TITLE.md`）．ダウンロード時のままの機械的なファイル名は残さない．

## 文書一覧

| ファイル | 時点 | 内容 | 読むとき注意すること |
| --- | --- | --- | --- |
| [d0001_literature_survey_2026-07.md](d0001_literature_survey_2026-07.md) | 2026-07-26 | 文献調査とリポジトリ実測の**一次記録**（出典付き）．評価解像度の統計，`max_seq_len` による学習データ切り詰め率の実測，VRAM の内訳（PLE 非量子化），分散 LoRA 集約手法の比較 | 「論文は〜と報告している」と「〜と考えられる」を区別して書いてある．解釈と出典を混同しないこと |
| [d0002_positioning_and_novelty_2026-07.md](d0002_positioning_and_novelty_2026-07.md) | 2026-07-12 | WAFL 系譜における本プロジェクトの位置付けと新規性の主張の材料．先行研究サーベイ，実装候補，期待される結果 | Iter14 で判明した「Iter13 まで P2P 重み交換が機能していなかった」事実を反映していない．知識収束を前提とする記述は再確認が必要 |

## 関連する記録の在り処

| 場所 | 役割 |
| --- | --- |
| `../README.md` | アーキテクチャ・使用方法・設定・プロトコル・ログ形式（実装の入口） |
| `../plans/p0001_research_direction_2026-07.md` | 研究方針の意思決定と提案（根拠は d0001） |
| `../.claude/research/journal.md` | イテレーションごとの仮説・実装・実験・分析・判定（直近 3 件） |
| `../.claude/research/journal_archive.md` | 古いイテレーションの記録 |
| `../.claude/research/backlog.md` | 人間判断待ち事項と，自動で下した暫定判断の記録 |
| `../.claude/research/config.yml` | research-cycle の設定．振る予定のレバー（W1〜W9）とその実施状況 |

比較図（複数実験を横断する誤差棒付き比較）は `src/compare_baselines.py` / `src/compare_runs.py` が
`results/fig07_baseline_comparison.png` へ出力する．`results/` は `.gitignore` 対象であり，
実験ごとに再生成される．
