# RTX 3060 12GB Windows PC を Ubuntu 化して H3 を速くする計画

作成: 2026-11-01。対象は RTX 3060 12GB / RAM 32GB の Windows PC で、ComfyUI 上の MiniMax H3 を Studio の生成先として使っている構成。GPU を買い替える前に、**質を落とさない施策から順に、1 つずつ A/B で確認しながら**速度を上げるための手順書。

## 0. 先に結論

| 問い | 答え |
|---|---|
| Windows より Ubuntu の方が有利か | **有利。ただし「Ubuntu にしただけで速くなる」のは一部**。Ubuntu が必須なのは gpu-unlock（Linux 専用カーネルモジュール）と、SageAttention / Triton 系のビルドの摩擦が無いこと、VRAM の予約が少なく 12GB をフルに使えること。速度の大半は後述の H3 向けチューニングから来る |
| 前回の「3060 で 1.5〜2 倍」メモは使えるか | **そのままは使えない**。あれは Wan 2.2（14B / 5B）のレシピ。このリポジトリの H3 は DiT が 33B（BF16 66GB）、テキストエンコーダが Qwen3-VL 32B（BF16 51GB）で、12GB には何をどうやっても収まらない。「5B を fp16 で素直に回す」という逃げ道が H3 には無い。Lightning LoRA の代わりは Comfy-Org 公式の Turbo LoRA と FastH3 蒸留チェックポイント |
| 12GB で速度を決める要因 | **VRAM に載らない重みを毎ステップ PCIe 越しに流す量**。計算速度より「1 ステップあたり何 GB をストリーミングしているか」が支配的。だから VRAM を 1GB 空ける施策は、質を一切変えずに速度へ直結する |
| 進め方 | Windows で基準を記録 → Ubuntu 導入 → 同一設定で同一出力を確認 → 無劣化の施策を 1 つずつ → 近似の施策を計測つきで → 意図的に質を落とす施策は用途別プロファイルとして分離 |

## 1. 前提の整理

### 1.1 ハードウェア

- GPU: RTX 3060 12GB（Ampere, SM86, 170W）。FP8 / FP4 の Tensor Core は無い。fp8 / nvfp4 の重みは読み込めるが、演算は BF16 に戻して行う
- RAM: 32GB。DiT とテキストエンコーダを同時に RAM へ置けないので、ComfyUI がモデルを順番に入れ替える前提
- PCIe: 世代とレーン数を Phase 0 で確認する（3.0 x16 と 4.0 x16 でストリーミング速度が倍違う）

### 1.2 H3 の重みと 12GB で選べる範囲

Comfy-Org 公式（`Comfy-Org/MiniMax-H3`）とコミュニティの主な選択肢。サイズはファイルサイズ。

| 部品 | 候補 | サイズ | 備考 |
|---|---|---|---|
| DiT | `minimax_h3_fl2va_pruned_int8_convrot` | 21.0GB | 公式。pruned の中では最も質が良いとされる量子化。12GB には載らず約 10GB をストリーミング |
| DiT | `minimax_h3_fl2va_pruned_w6a8` | 16.0GB | 公式。int8 より軽く、やや劣化 |
| DiT | `minimax_h3_fl2va_pruned_fp8_scaled` | 21.0GB | 公式。Ampere では fp8 の利点が無いので候補から外す |
| DiT | `MiniMax-H3-FL2VA-Pruned-Q5_K_M.gguf` | 14.1GB | コミュニティ（Abiray）。ComfyUI-GGUF が必要 |
| DiT | `MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf` | 11.6GB | 同上。ほぼ VRAM に収まる唯一の pruned 系 |
| DiT | `MiniMax-H3-FL2VA-Pruned-Q3_K_M.gguf` | 8.9GB | 同上。全部 VRAM に載るが Q3 の劣化は大きい |
| DiT | `minimax_h3_fl2va-Q3_K_M.gguf`（非 pruned） | 15.6GB | コミュニティ（vantagewithai）。「pruned で軽い量子化」vs「非 pruned で重い量子化」の比較用 |
| テキストエンコーダ | `qwen3vl_32b_minimax_h3_nvfp4_awq` | 15.7GB | 公式。3060 12GB / 32GB RAM ユーザーが使っている構成。Ampere では逆量子化して実行 |
| テキストエンコーダ | `qwen3vl_32b_minimax_h3_int8_convrot` | 27.1GB | RAM 32GB では OS と合わせて危険。RAM 64GB 化したときの候補 |
| 動画 VAE | `minimax_h3_video_vae_fp16` / `_int8_convrot` | 5.2GB / 2.8GB | int8 版は kijai の実験版。VAE デコードは 12GB で OOM しやすい箇所 |
| 音声 VAE | `minimax_h3_audio_vae_fp32` | 0.6GB | 共通 |
| Turbo LoRA | `minimax_h3_fl2v_turbo_8step_v1.0` / `_4step` / `ref2v_turbo_4step` | 2.0GB | 公式。意図的に質を売る施策 |
| 蒸留モデル | FastVideo FastH3 8step v2 pruned int8_convrot | — | 公式配布。T2V / FL2V のみ、Ref2VA 非対応。VSA（ブロックスパース）前提 |

**pruned 自体が既に質を落とした派生**（BF16 で 66GB → 40GB）である点は意識しておく。ただし非 pruned を 12GB / 32GB で回す手段は実質無いので、ここでは pruned を「12GB での上限」として扱う。

### 1.3 3060 12GB の既知の実績

- 864x480 / 5 秒 / Turbo 8step で 4.5 分（Comfy-Org HF discussion #35、2026-08、int8 VAE 使用）
- 960x544 / Turbo 4step LoRA / 5 steps で動作報告あり（同 #41）
- 同じユーザー層がテキストエンコーダに nvfp4（int4）版を使用

つまり「Turbo を使えば実用、ベースモデルの全ステップは重い」という状況。この計画は **ベースモデルの全ステップをどこまで速くできるか**（＝質を保ったまま）を主眼に置き、Turbo は最後に用途別の選択肢として置く。

## 2. 施策の分類と比較のルール

### 2.1 3 つのバケツ

| バケツ | 定義 | 判定方法 | 施策 |
|---|---|---|---|
| A. 無劣化 | 出力がビット一致、または同一設定の再実行で生じる揺らぎ（ノイズフロア）以下 | `--deterministic` で同 seed を回し、フレームの PSNR が無限大 or ノイズフロア以下 | OS 移行、ヘッドレス化、`--reserve-vram`、VAE タイルデコード、Chunk FeedForward、Fused Modulation、gpu-unlock |
| B. 近似（要計測） | 設計上わずかに数値が変わる。閾値次第で見えない〜見える | 同 seed で A/B。PSNR / SSIM と目視、動き量、音声差分 | SageAttention、Sol-Attn（tau）、EasyCache、`fp16_accumulation`、量子化の段階、int8 VAE |
| C. 意図的な劣化 | 出力の性格が変わる（ステップ蒸留・スパース化） | 用途別プロファイルとして分離し、本番には使わない or 明示的に選ぶ | Turbo LoRA、FastH3、Spectrum、解像度・尺の削減 |

### 2.2 比較の原器

- **プロンプト 3 本を固定**: (1) 人物のセリフあり（音声の質を見る）、(2) カメラと被写体が大きく動く（動きが死ぬ劣化を見る）、(3) 静的・建築/プロダクト（ディテールを見る）。Studio の実投入プロンプトから選ぶ
- **seed 固定、解像度・フレーム数固定**（例: 768x432 相当 / 6 秒 = 149 フレーム。H3 は 17k+5 フレーム）
- **REF**: その時点で回せる最高品質設定（pruned int8_convrot、ベースのステップ数、キャッシュ無し、SDPA）。バケツ B/C はすべて REF と比較する
- **ノイズフロア**: 変更前に REF を 2 回回して A と A' の差を測る。これ以下の差は「無劣化」と判定する

### 2.3 記録する指標

| 指標 | 取り方 |
|---|---|
| 生成時間 | ComfyUI ログの `Prompt executed in N seconds`。サンプラー部分は進捗バーの s/it |
| ピーク VRAM | `nvidia-smi --query-gpu=memory.used,utilization.gpu,clocks.sm,power.draw --format=csv -lms 500 > vram.csv` を生成中に回す |
| ピーク RAM | `free -m` を同じ要領で、または `/usr/bin/time -v` |
| 出力フレーム | 動画保存ノードと並列に `SaveImage` を置いて PNG 連番で保存（MP4 のエンコードを経由しない比較用） |
| 音声 | `ffmpeg -i out.mp4 -vn -c:a pcm_s16le out.wav` で取り出し、`cmp` で一致確認、不一致なら試聴 |
| 動き量 | 「動きが死ぬ」劣化の検出用プロキシ。フレーム間差分の平均輝度を見る |

```bash
# フレーム列の比較（a/ と b/ に同名の PNG がある前提）
ffmpeg -i a/%05d.png -i b/%05d.png -lavfi "psnr;[0:v][1:v]ssim" -f null - 2>&1 | grep -E "PSNR|SSIM"

# 動き量のプロキシ（値が大きいほど動いている。A/B で比べる）
ffmpeg -i out.mp4 -vf "tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG" \
  -f null - 2>&1 | grep -o "YAVG=[0-9.]*" | cut -d= -f2 | awk '{s+=$1} END {print s/NR}'
```

### 2.4 記録テンプレート

結果はこの表に追記する（`docs/` 内か Studio のメモ欄）。1 行 1 変更で、**同時に 2 つ変えない**。

| # | 変更 | バケツ | 時間 (s) | s/it | VRAM peak | RAM peak | PSNR vs 前段 | 動き量 | 音声 | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | Windows 基準 | — | | | | | — | | | 原器 |
| 1 | Ubuntu 同一設定 | A | | | | | | | | |

## 3. Phase 0 — Windows で基準を記録する（移行前に必ず）

今動いている Windows 環境が唯一の「動く実績」なので、壊す前に全部記録する。

1. ComfyUI の **Export (API)** で現行ワークフローを保存。Studio に読み込み済みなら `data/studio/comfy/*.json` にもある
2. 使っているモデルファイル名とサイズを控える（`models/diffusion_models`, `text_encoders`, `vae`, `loras`）
3. `custom_nodes/` の一覧と `pip freeze` を保存
4. 起動オプション（`--lowvram` / `--reserve-vram` / `--use-sage-attention` 等）と ComfyUI のバージョンを控える
5. §2.2 のプロンプト 3 本を seed 固定で生成し、時間・VRAM・RAM と PNG 連番・音声を保存。これが「0: Windows 基準」
6. ハードウェア情報: `nvidia-smi -q | findstr /i "PCIe Generation Link Width"` で PCIe 世代とレーン、マザーボード型番、SSD の空き、iGPU の有無（CPU 型番）

ここで保存した API JSON は、Ubuntu 側でも Studio でもそのまま使う。

## 4. Phase 1 — Ubuntu の導入

### 4.1 構成の決定

- **デュアルブート。Windows は消さない**。Windows の ComfyUI が原器であり、Ubuntu 側で詰まったときの退避先
- Ubuntu 24.04 LTS
- **Ubuntu 用に別の SSD か ≥ 500GB のパーティション**。モデルだけで int8 DiT 21GB + nvfp4 TE 15.7GB + VAE 6GB + GGUF 比較用 25GB + FastH3 + LoRA で 100GB 近くなる。Windows 側 NTFS のモデルを読むことも可能だが、ストリーミング元になるファイルは ext4 の NVMe に置く
- **ヘッドレス運用を推奨**。Studio は Mac で動かし、Ubuntu 機には SSH と ComfyUI の HTTP だけ。3060 が画面描画を持たないので VRAM が 0.5〜1GB 空き、コンポジタによるカクつきも消える。デスクトップが必要なら iGPU に画面を出す（CPU に iGPU があれば BIOS で Primary Display を iGPU に）

### 4.2 Windows 側の事前作業（ここで詰まる人が多い）

1. **BitLocker**: 有効なら回復キーを控えてから一時停止。Secure Boot の変更で回復モードに入る
2. **高速スタートアップを無効化**（電源オプション）。有効のままだと NTFS がハイバネーションでロックされ、Ubuntu から読めない
3. **Secure Boot を BIOS で無効化**。gpu-unlock はツリー外のカーネルモジュールなので、Secure Boot 有効だと MOK 署名が必要になる。無効の方が NVIDIA ドライバも含めて手間が少ない
4. Windows の時計を UTC にするか（レジストリ `RealTimeIsUniversal`）、Ubuntu 側で `timedatectl set-local-rtc 1`。デュアルブートの時刻ずれ対策
5. ディスク管理で Ubuntu 用の未割り当て領域を確保（別 SSD なら不要）

### 4.3 Ubuntu のインストールと基本設定

```bash
# インストール後
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y build-essential git curl ffmpeg python3.12-venv python3-pip openssh-server linux-headers-$(uname -r)

# NVIDIA ドライバ（Ubuntu 推奨版。Ampere は open カーネルモジュールで可）
sudo ubuntu-drivers devices
sudo ubuntu-drivers install
sudo reboot
nvidia-smi

# ヘッドレス化（デスクトップを入れた場合）
sudo systemctl set-default multi-user.target
```

CUDA Toolkit は **ドライバとは別に、SageAttention のビルド用にだけ** 入れる。torch の CUDA 版（cu128）と nvcc のメジャーを合わせる。

```bash
# NVIDIA の apt リポジトリを追加後
sudo apt install -y cuda-toolkit-12-8
echo 'export PATH=/usr/local/cuda-12.8/bin:$PATH' >> ~/.bashrc
```

### 4.4 ComfyUI のセットアップ（Windows と同じ構成を再現する）

```bash
git clone https://github.com/comfyanonymous/ComfyUI ~/ComfyUI && cd ~/ComfyUI
python3 -m venv .venv && source .venv/bin/activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt

cd custom_nodes
git clone https://github.com/Comfy-Org/ComfyUI-Manager
git clone https://github.com/city96/ComfyUI-GGUF            # GGUF を使う場合
git clone https://github.com/kijai/ComfyUI-KJNodes           # H3 用 Sage パッチ / Low VRAM Attention
git clone https://github.com/Saganaki22/ComfyUI-sol-attn     # Chunk FeedForward / Fused Modulation / Sol-Attn
# 各 custom_node の requirements.txt を pip install
```

モデルは Windows で使っていたものと **同じファイル** を `models/` に置く。systemd で常駐させ、LAN からだけ届くようにする:

```ini
# /etc/systemd/system/comfyui.service
[Unit]
Description=ComfyUI
After=network-online.target

[Service]
User=<user>
WorkingDirectory=/home/<user>/ComfyUI
ExecStart=/home/<user>/ComfyUI/.venv/bin/python main.py --listen 0.0.0.0 --port 8188
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8188 proto tcp
sudo ufw allow from 192.168.0.0/16 to any port 22 proto tcp
sudo ufw enable
sudo systemctl enable --now comfyui
```

起動オプションは Phase 0 で控えた Windows と同じにする。`--use-sage-attention` や `--fast` は **この時点ではまだ付けない**。

### 4.5 Phase 1 の完了条件

Windows の API JSON をそのまま投入し、§2.2 の 3 本が生成できること。これが表の「1: Ubuntu 同一設定」。Windows 基準との差が、**OS 移行だけの効果**。VRAM ピークが Windows より下がっているはず（WDDM の予約分）。

## 5. Phase 2 — 無劣化の施策（バケツ A）

1 つ適用するごとに §2.2 の 3 本を回し、前段と PSNR 比較して記録する。判定用の実行には `--deterministic` を付け、本番では外す。順番は「安い・効く」順。

| # | 施策 | やること | 期待 | 確認 |
|---|---|---|---|---|
| A1 | ヘッドレス / 表示を 3060 から外す | §4.1。`nvidia-smi` で起動直後の使用量が 100MB 未満になること | VRAM 0.5〜1GB 回復 → ストリーミング量が減る | ビット一致 |
| A2 | `--reserve-vram` の調整 | `--lowvram` は使わない。`--reserve-vram 0.5` から 0.25 刻みで下げ、OOM せず最も速い値 | 常駐できる重みが増える | ビット一致 |
| A3 | VAE デコードのタイル化 | `VAEDecodeTiled` に差し替え。タイルサイズは大きいほど継ぎ目が出にくい | VAE デコード時の OOM / ストリーミング回避 | PSNR で継ぎ目の影響を確認（厳密にはバケツ B 寄り） |
| A4 | Chunk FeedForward | ComfyUI-sol-attn の `MiniMax H3 Chunk FeedForward` ノード | MLP のピーク VRAM −37%（作者計測）。出力はビット一致と明記 | ビット一致 |
| A5 | Fused Modulation | 同 `MiniMax H3 Fused Modulation`。KJNodes の Low VRAM Attention とは併用不可 | 要素演算の融合。ビット一致と明記 | ビット一致 |
| A6 | gpu-unlock | §5.1 | BF16×FP32 累算の Tensor Core レートが約 2 倍。ただし 12GB ではストリーミング律速なので全体への効果は要計測 | ビット一致（README 明記） |

A1〜A5 で VRAM に余裕ができたら、**Phase 3 の量子化ラダーで 1 段上の量子化（int8 ← w6a8 ← Q5 ← Q4）が同じ速度で回るようになる**のが本当の狙い。「速くする」だけでなく「同じ速度でより質の高い重みを使う」方向にも使える。

### 5.1 gpu-unlock の手順と確認

```bash
git clone https://github.com/Zagitta/gpu-unlock ~/gpu-unlock && cd ~/gpu-unlock
make install            # Debian 系を検出して deb を作り、インストールする
sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 gpu_unlock.targets=all"/' /etc/default/grub
sudo update-grub && sudo reboot

# 確認
journalctl -k -b | grep gpu_unlock
```

H3 を回す前に、GEMM 単体で効いているかを 10 秒で確認する:

```bash
python - <<'PY'
import torch, time
a = torch.randn(8192, 8192, device="cuda", dtype=torch.bfloat16)
b = torch.randn(8192, 8192, device="cuda", dtype=torch.bfloat16)
for _ in range(3): a @ b
torch.cuda.synchronize(); t = time.time()
for _ in range(20): a @ b
torch.cuda.synchronize(); dt = (time.time() - t) / 20
print(f"{2 * 8192**3 / dt / 1e12:.1f} TFLOPS")
PY
```

3060 12GB の README 値は BF16 で 27 → 51 TFLOPS。ここが上がっていないなら、H3 を回しても意味が無い。注意点:

- 本家は Linux カーネルモジュールのみ。**公式のインストーラ・サイト・コインは存在しない**。GitHub の `Zagitta/gpu-unlock` 以外から入れない
- サスペンドから復帰すると自動で再適用される。ハイバネーションは非対応
- 発熱は増えるが、電力制限と温度制御はそのまま効く。ベンチ中に `nvidia-smi` のクロックとスロットル理由を見ておく
- 止めるときはカーネルパラメータを外すだけ

## 6. Phase 3 — 近似の施策（バケツ B、計測つき）

ここからは数値が変わる。**REF（Phase 2 完了時点の構成）と比較して、PSNR・目視・動き量・音声の 4 点で判断する**。閾値のある施策は弱い側から入る。

| # | 施策 | 設定 | 3060 での注意 |
|---|---|---|---|
| B1 | 量子化ラダー | int8_convrot（REF）→ w6a8 → GGUF Q5_K_M → Q4_K_M。それぞれの速度と PSNR vs REF | **速いのは「VRAM に載る方」とは限らない**。GGUF は毎ステップ逆量子化のコストがあり、int8 はストリーミングのコストがある。PCIe 4.0 なら int8 が、3.0 なら GGUF が勝ちやすい。測るまで分からない |
| B1' | pruned vs 非 pruned | 非 pruned Q3_K_M（15.6GB）vs pruned Q5_K_M（14.1GB） | 「枝刈り」と「量子化」のどちらが H3 の質を削るかの比較。RAM 32GB で両方回る |
| B2 | SageAttention | `pip install sageattention==2.2.0 --no-build-isolation` → `--use-sage-attention`。H3 には KJNodes の `MiniMax H3 Memory Efficient Sage Attention Patch` も併用 | **Ampere には FP8 が無いので、QK は INT8、PV は FP16 の経路**。Ada 以降で報告される倍率は出ない。効果は要計測 |
| B3 | Sol-Attn（スパース） | ComfyUI-sol-attn の `MiniMax H3 Scheduled Sol Attention Patch`。tau を低め（密）から。順序は KJ Sage → KJ H3 Sage → Sol の順が必須 | v0.6.2 で SM86 対応、3090 Ti で動作確認済み、**SM86 の性能は未計測**。作者の 5090 計測では Sage 比 1.14〜1.44 倍。誤差は相対 L2 0.008 |
| B4 | EasyCache / LazyCache | ComfyUI 本体ノード。`reuse_threshold` を小さい値から。H3 の音声/映像の二出力に対応している | TeaCache の後継で H3 対応はこれだけ。コミュニティ報告で約 25%。**Sol の tau と EasyCache の閾値を同時に上げない**（作者も明記） |
| B5 | `--fast fp16_accumulation` | PyTorch 2.7 以降 | 累算を FP16 にする＝精度を落とす。**gpu-unlock が効いている状態では旨味が減る**（制限されているのは FP32 累算の経路なので）。A6 の後に測って、差が無ければ外す |
| B6 | int8 動画 VAE | `minimax_h3_video_vae_int8_convrot` | kijai の実験版。VAE デコードの VRAM を半分にするが、最終画質に直接触る部分なので PSNR を厳しめに見る |

B1 の結果が「本番用の重み」を決める。残りはその重みの上で測る。

## 7. Phase 4 — 意図的な劣化（バケツ C、用途別プロファイル）

本番用ではなく **試作・構図確認・テイク数を稼ぐ**ための設定として、REF と別のワークフロー JSON に分けて Studio に登録する（Studio は読み込んだワークフローの設定を保持するので、JSON を切り替えるだけで済む）。

| # | 施策 | 内容 | 質のコスト |
|---|---|---|---|
| C1 | Turbo 8step LoRA（公式） | `LoraLoaderModelOnly` で `minimax_h3_fl2v_turbo_8step_v1.0` を読み、ステップを 8 に。Ref2VA は `ref2v_turbo_4step` | 動きの振幅とプロンプト追従が落ちる。CFG 蒸留済みモデルなので negative は元々効かない点は変わらない。3060 で 864x480 / 5 秒が 4.5 分の実績 |
| C2 | Turbo 4step LoRA（公式） | 同上を 4〜5 step | C1 よりさらに平均化。構図確認専用 |
| C3 | FastH3（公式配布の蒸留モデル） | `fastvideo_fasth3_8step_v2_pruned_int8_convrot` + `BlockSparseAttention`（VSA keep 10%）。ステップは 8 固定 | 公式ドキュメントが「動きと音声の忠実度を速度と交換」「Ref2VA 非対応」と明記。T2V / FL2V の試作用 |
| C4 | Spectrum ノード | `ComfyUI-Spectrum-MiniMax-H3` | 報告が −2 倍〜+2 倍まで割れている近似加速。入れるなら同 seed で必ず A/B |
| C5 | 解像度・尺 | 試作 480p / 49〜81 フレーム、本番だけ 768 短辺 / 実尺 | 質の劣化ではなく成果物の仕様。H3 の学習解像度は短辺 768 なので、それ以上は遅くなるだけ |

Studio 側には **「本番（REF + 無劣化 + 採用した B）」と「試作（C1 or C3）」の 2 本**を登録し、コマ割りの確認は試作、承認する完成版は本番で撮り直す運用にする。

## 8. Studio への接続

Ubuntu 機をヘッドレスにした場合、Studio は Mac で動かし `.env.studio` を書き換える:

```dotenv
COMFYUI_BASE_URL=http://<ubuntu-ip>:8188
```

Studio は参照画像を `/upload/image` でアップロードするので、Mac と Ubuntu 機の間でフォルダ共有は不要。Ubuntu 側で Export (API) した JSON を「生成環境の設定」から読み込む。手順は [docs/studio.md](studio.md) の「すでに動いている ComfyUI H3 に接続」と同じ。

## 9. 期待値と判断基準

正直な見積もり。**すべて要計測で、12GB ではストリーミング律速のため、計算側の施策（gpu-unlock、Sage）は数字より小さく出る可能性が高い**。

| 施策 | 期待 | 根拠 |
|---|---|---|
| OS 移行のみ | ±0〜15% | WDDM の VRAM 予約が無くなる分。オフロードの挙動が予測可能になる |
| A1〜A5（VRAM 回復） | 1 ステップのストリーミング量に比例 | 空いた VRAM 分だけ PCIe 転送が減る。PCIe 3.0 ほど効く |
| A6 gpu-unlock | 計算部は最大 2 倍、全体は要計測 | 本家 README の 3060 値は BF16 27→51 TFLOPS。動画生成の実測は +15〜20%（Wan 2.2） |
| B2 Sage（Ampere） | 要計測 | FP8 経路が無い |
| B3 Sol-Attn | Sage 比 1.1〜1.4 倍（5090 値） | SM86 は未計測 |
| B4 EasyCache | 〜25% | コミュニティ報告 |
| C1 Turbo 8step | サンプラー部で ×5 前後 | ステップ数比 |

**GPU 交換・RAM 増設の判断**は Phase 3 が終わった時点で行う:

- 不満が「速度」で、Turbo を試作に使えば回る → 現状維持
- 不満が「本番の質」で、B1 で int8_convrot が遅すぎて使えない → **まず RAM を 64GB に**（DDR4 なら安い）。int8 テキストエンコーダと重い量子化の DiT を RAM に置けるようになり、ディスクからの読み直しが消える
- 不満が「解像度・尺・Ref2VA の質」で VRAM が根本原因 → GPU 交換。その場合もここで作った計測プロトコルと本番/試作の 2 プロファイルはそのまま使える

## 10. 参考

- gpu-unlock: https://github.com/Zagitta/gpu-unlock（Linux 専用。Windows 移植は別プロジェクトで実験段階）
- Comfy-Org H3 重み一覧: https://huggingface.co/Comfy-Org/MiniMax-H3
- ComfyUI H3 ガイド: https://docs.comfy.org/tutorials/video/minimax/minimax-h3
- FastH3: https://docs.comfy.org/tutorials/video/minimax/minimax-h3-fastvideo
- ComfyUI-sol-attn（Chunk FF / Fused Modulation / Sol-Attn、SM86 対応）: https://github.com/Saganaki22/ComfyUI-sol-attn
- SageAttention: https://github.com/thu-ml/SageAttention
- pruned GGUF: https://huggingface.co/Abiray/MiniMax-H3-Pruned-GGUF
- 3060 12GB の実績スレッド: https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/35 / https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/41
