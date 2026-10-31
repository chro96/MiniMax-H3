# RTX 3060 12GB Windows PC を Ubuntu 化して H3 を速くする計画

作成: 2026-11-01。対象は RTX 3060 12GB / RAM 32GB の Windows PC で、ComfyUI 上の MiniMax H3 を Studio の生成先として使っている構成。GPU を買い替える前に、**12GB で実用になる「塩梅」を見つけ、その点を計測しながら詰める**ための手順書。

## 0. 先に結論

| 問い | 答え |
|---|---|
| Windows より Ubuntu の方が有利か | **有利。ただし「Ubuntu にしただけで速くなる」のは一部**。Ubuntu が必須なのは gpu-unlock（Linux 専用カーネルモジュール）と、SageAttention / Triton 系のビルドの摩擦が無いこと、VRAM の予約が少なく 12GB をフルに使えること。速度の大半は後述の H3 向けチューニングから来る |
| 「3060 で 1.5〜2 倍」のメモは使えるか | **そのままは使えない**。あれは Wan 2.2（14B / 5B）のレシピ。このリポジトリの H3 は DiT が 33B（BF16 66GB）、テキストエンコーダが Qwen3-VL 32B（BF16 51GB）で、12GB には何をどうやっても収まらない。「5B を fp16 で素直に回す」という逃げ道が H3 には無い。Lightning LoRA の代わりは Comfy-Org 公式の Turbo LoRA と FastH3 蒸留チェックポイント |
| 12GB / 32GB で「無劣化構成」は使えるか | **使えない**。pruned int8 をベースのステップ数で回す構成は、この機材では速度が出ず本番運用に耐えない。本番構成は量子化と Turbo を含む妥協点になる前提で計画する。無劣化構成は**品質の物差しとして一度だけ回す**サンプルに使う |
| 進め方 | Windows で基準を記録 → Ubuntu 導入 → **どの構成でも効く共通チューニングを先に全部入れる** → 量子化 × ステップ × 解像度の「塩梅」を探す → 決めた点で個別チューニング |

## 1. 前提の整理

### 1.1 ハードウェア

- GPU: RTX 3060 12GB（Ampere, SM86, 170W）。FP8 / FP4 の Tensor Core は無い。fp8 / nvfp4 の重みは読み込めるが、演算は BF16 に戻して行う
- RAM: 32GB。DiT とテキストエンコーダを同時に RAM へ置けないので、ComfyUI がモデルを順番に入れ替える前提
- PCIe: 世代とレーン数を Phase 0 で確認する（3.0 x16 と 4.0 x16 でストリーミング速度が倍違う）

### 1.2 12GB で速度を決めるもの

VRAM に載らない重みを**毎ステップ PCIe 越しに流す量**が支配的。計算が速くなっても効きにくく、VRAM を 1GB 空けると質を変えずに速度が上がる。速度を決める軸は 3 つで、塩梅探しはこの 3 軸の探索になる:

1. **DiT のサイズ**（量子化の段階）— ストリーミング量を決める
2. **ステップ数**（ベース vs Turbo 8 / 4）— ストリーミングの回数を決める
3. **解像度 × フレーム数**— 1 ステップの計算量と VAE デコードの VRAM を決める

### 1.3 H3 の重みと 12GB で選べる範囲

Comfy-Org 公式（`Comfy-Org/MiniMax-H3`）とコミュニティの主な選択肢。サイズはファイルサイズ。

| 部品 | 候補 | サイズ | 備考 |
|---|---|---|---|
| DiT | `minimax_h3_fl2va_pruned_int8_convrot` | 21.0GB | 公式。pruned の中では最も質が良いとされる量子化。約 10GB をストリーミング。**物差し用** |
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
| Turbo LoRA | `minimax_h3_fl2v_turbo_8step_v1.0` / `_4step` / `ref2v_turbo_4step` | 2.0GB | 公式。ステップ蒸留 |
| 蒸留モデル | FastVideo FastH3 8step v2 pruned int8_convrot | — | 公式配布。T2V / FL2V のみ、Ref2VA 非対応。VSA（ブロックスパース）前提 |

**pruned 自体が既に質を落とした派生**（BF16 で 66GB → 40GB）である点は意識しておく。ただし非 pruned を 12GB / 32GB で回す手段は実質無いので、ここでは pruned を「12GB での上限」として扱う。

### 1.4 3060 12GB の既知の実績

- 864x480 / 5 秒 / Turbo 8step で 4.5 分（Comfy-Org HF discussion #35、2026-08、int8 VAE 使用）
- 960x544 / Turbo 4step LoRA / 5 steps で動作報告あり（同 #41）
- 同じユーザー層がテキストエンコーダに nvfp4（int4）版を使用

**これが塩梅探しの出発点**。この計画はここから「同じ時間でどこまで質を上げられるか」「許容時間の中で何を取るか」を決める。

## 2. 施策の分類と比較のルール

### 2.1 施策は「いつ入れるか」で 3 群に分ける

| 群 | 定義 | 施策 | 入れる段階 |
|---|---|---|---|
| **共通** | どの重み・ステップ数・解像度でも効く。構成を決める前に入れてしまってよい | ヘッドレス化、`--reserve-vram`、VAE タイルデコード、Chunk FeedForward、Fused Modulation、SageAttention、gpu-unlock | Phase 2 |
| **塩梅の軸** | 速度と質のトレードオフそのもの。組み合わせで「構成」が決まる | DiT の量子化段階、ベース / Turbo 8 / Turbo 4 / FastH3、解像度・フレーム数 | Phase 3 |
| **個別** | 決めた構成の上で効き方が変わる。構成ごとに閾値を詰める | EasyCache 閾値、Sol-Attn の tau、LoRA 強度・ステップ数の微調整、int8 VAE、`fp16_accumulation`、Spectrum | Phase 4 |

共通群のうち、ヘッドレス / reserve-vram / VAE タイル / Chunk FF / Fused Modulation / gpu-unlock は出力が**ビット一致**（または設計上同等）。SageAttention だけは近似だが、H3 での劣化が小さく構成に依らず効くので共通群に入れる。

### 2.2 比較の原器

- **プロンプト 3 本を固定**: (1) 人物のセリフあり（音声の質を見る）、(2) カメラと被写体が大きく動く（動きが死ぬ劣化を見る）、(3) 静的・建築/プロダクト（ディテールを見る）。Studio の実投入プロンプトから選ぶ
- **seed 固定**
- **スモーククリップ**: 共通群の確認用。480p 級 / 2 秒（53 フレーム）。速く回して判定する
- **本番クリップ**: 塩梅探しと個別チューニング用。Studio の実寸（6 秒 = 149 フレーム、幅・高さは現行ワークフロー値）
- **CEILING（物差し）**: pruned int8_convrot、ベースのステップ数、キャッシュ無し、SDPA で本番クリップ 3 本を**一度だけ**生成する。一晩かかってもよい。これは本番構成ではなく、「この機材で出せる最高」のサンプル。塩梅探しの PSNR と目視はこれと比べる
- **FLOOR**: Windows の現行構成の出力。「今より悪くなっていないか」の下限
- **ノイズフロア**: 同じ構成を 2 回回して A と A' の差を測る。これ以下の差は「同等」と判定する

### 2.3 時間の予算

塩梅を決めるには「何分までなら許せるか」が先に要る。Studio は標準 5 カット × 6 秒で、撮り直しが前提の運用なので、**1 カットあたり**で決める。初期値は仮置きで、Phase 3 の最初に見直す:

| プロファイル | 用途 | 1 カットの予算（仮） |
|---|---|---|
| 試作 | コマ割り・構図・タイミングの確認。テイク数を稼ぐ | ≤ 5 分 |
| 本番 | 承認する完成版の撮り直し | ≤ 15〜20 分 |

### 2.4 記録する指標

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

### 2.5 記録テンプレート

結果はこの表に追記する。1 行 1 変更で、**同時に 2 つ変えない**（Phase 3 の塩梅探しも軸を 1 つずつ動かす）。

| # | 変更 | 群 | クリップ | 時間 (s) | s/it | VRAM peak | RAM peak | PSNR vs 前段 | PSNR vs CEILING | 動き量 | 音声 | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | Windows 現行（FLOOR） | — | 本番 | | | | | — | | | | 下限 |
| 1 | Ubuntu 同一設定 | — | 本番 | | | | | | | | | |

## 3. Phase 0 — Windows で基準を記録する（移行前に必ず）

今動いている Windows 環境が唯一の「動く実績」なので、壊す前に全部記録する。

1. ComfyUI の **Export (API)** で現行ワークフローを保存。Studio に読み込み済みなら `data/studio/comfy/*.json` にもある
2. 使っているモデルファイル名とサイズを控える（`models/diffusion_models`, `text_encoders`, `vae`, `loras`）
3. `custom_nodes/` の一覧と `pip freeze` を保存
4. 起動オプション（`--lowvram` / `--reserve-vram` / `--use-sage-attention` 等）と ComfyUI のバージョンを控える
5. §2.2 のプロンプト 3 本を seed 固定で生成し、時間・VRAM・RAM と PNG 連番・音声を保存。これが FLOOR
6. ハードウェア情報: `nvidia-smi -q | findstr /i "PCIe Generation Link Width"` で PCIe 世代とレーン、マザーボード型番、SSD の空き、iGPU の有無（CPU 型番）

ここで保存した API JSON は、Ubuntu 側でも Studio でもそのまま使う。

## 4. Phase 1 — Ubuntu の導入

### 4.1 構成の決定

- **デュアルブート。Windows は消さない**。Windows の ComfyUI が FLOOR であり、Ubuntu 側で詰まったときの退避先
- Ubuntu 24.04 LTS
- **Ubuntu 用に別の SSD か ≥ 500GB のパーティション**。モデルだけで int8 DiT 21GB + nvfp4 TE 15.7GB + VAE 6GB + 塩梅探し用の GGUF / w6a8 で 50GB + FastH3 + LoRA で 100GB を超える。Windows 側 NTFS のモデルを読むことも可能だが、ストリーミング元になるファイルは ext4 の NVMe に置く
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

Windows の API JSON をそのまま投入し、§2.2 の 3 本が生成できること。これが表の「1: Ubuntu 同一設定」。FLOOR との差が、**OS 移行だけの効果**。VRAM ピークが Windows より下がっているはず（WDDM の予約分）。

## 5. Phase 2 — 共通チューニング（構成を決める前に全部入れる）

どの重み・ステップ数でも効くものを、**スモーククリップで 1 つずつ確認しながら**まとめて入れる。ここで空いた VRAM が、Phase 3 で 1 段上の量子化や 1 段上の解像度を同じ時間で回せる余地になる。判定用の実行には `--deterministic` を付け、本番では外す。

| # | 施策 | やること | 期待 | 確認 |
|---|---|---|---|---|
| A1 | ヘッドレス / 表示を 3060 から外す | §4.1。`nvidia-smi` で起動直後の使用量が 100MB 未満になること | VRAM 0.5〜1GB 回復 → ストリーミング量が減る | ビット一致 |
| A2 | `--reserve-vram` の調整 | `--lowvram` は使わない。`--reserve-vram 0.5` から 0.25 刻みで下げ、OOM せず最も速い値 | 常駐できる重みが増える | ビット一致 |
| A3 | VAE デコードのタイル化 | `VAEDecodeTiled` に差し替え。タイルサイズは大きいほど継ぎ目が出にくい | VAE デコード時の OOM / ストリーミング回避。本番解像度で効く | PSNR で継ぎ目の影響を確認 |
| A4 | Chunk FeedForward | ComfyUI-sol-attn の `MiniMax H3 Chunk FeedForward` ノード | MLP のピーク VRAM −37%（作者計測）。出力はビット一致と明記 | ビット一致 |
| A5 | Fused Modulation | 同 `MiniMax H3 Fused Modulation`。KJNodes の Low VRAM Attention とは併用不可 | 要素演算の融合。ビット一致と明記 | ビット一致 |
| A6 | SageAttention | `pip install sageattention==2.2.0 --no-build-isolation` → `--use-sage-attention`。H3 には KJNodes の `MiniMax H3 Memory Efficient Sage Attention Patch` も併用 | Attention の計算と VRAM を削る。**Ampere には FP8 が無いので QK は INT8、PV は FP16 の経路**。Ada 以降の倍率は出ない | PSNR vs 前段。劣化が見えたら外す |
| A7 | gpu-unlock | §5.1 | BF16×FP32 累算の Tensor Core レートが約 2 倍。ストリーミング律速なので全体への効果は要計測。**Turbo で ステップ数が減るほど、1 ステップの計算比率が上がり効きやすくなる** | ビット一致（README 明記） |

Phase 2 が終わったら、**本番クリップで Windows と同じ構成**をもう一度回して記録する。これが「共通チューニングだけでどれだけ速くなったか」の数字で、Phase 3 の出発点。

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

## 6. Phase 3 — 塩梅を探す（量子化 × ステップ × 解像度）

### 6.1 物差しを先に作る

CEILING（pruned int8_convrot、ベースのステップ数、キャッシュ無し、SDPA）で本番クリップ 3 本を一晩回して保存する。**この構成を本番に使うつもりは無い**。Phase 3 の全部の行は、FLOOR（今の Windows）より良いか、CEILING にどれだけ近いかで見る。

### 6.2 探索の進め方

全組み合わせは回さない。**既知の 3060 動作点から出発し、軸を 1 つずつ「質が上がる方向」に動かして、予算（§2.3）を超えたら戻す**。

```text
出発点: pruned Q4_K_M（or w6a8） × Turbo 8step × 864x480 級   ← 3060 の実績構成
  ├ 軸1 量子化:  Q4_K_M → Q5_K_M → w6a8 → int8_convrot   （時間と PSNR vs CEILING を記録）
  ├ 軸2 ステップ: Turbo 8 → Turbo 8 を 10〜12 step → ベース   （Turbo LoRA はステップを増やしても使える）
  └ 軸3 解像度:  480 級 → 544 級 → 短辺 768（H3 の学習解像度。これ以上は遅くなるだけ）
```

各軸で見るもの:

| 軸 | 速度への効き方 | 質への効き方 | 3060 での注意 |
|---|---|---|---|
| 量子化 | ストリーミング量（int8 21GB → 約 10GB 流す。Q4 11.6GB → ほぼ常駐）。**GGUF は毎ステップ逆量子化のコスト、int8 は転送のコスト**で、PCIe 4.0 なら int8 が、3.0 なら GGUF が勝ちやすい | Q4 → Q5 → w6a8 → int8 の順に CEILING に近づく。拡散モデルは誤差がステップで増幅されるので Q4 以下は LLM より劣化が大きい | 試作と本番で違う量子化を使ってよい（Studio にワークフローを 2 本登録する） |
| ステップ | ほぼ比例 | Turbo は動きの振幅とプロンプト追従が落ちる。H3 は CFG 蒸留済みなので negative は元々効かない | 公式 Turbo 8step が出発点。4step は構図確認専用。FastH3 は T2V / FL2V のみで Ref2VA 非対応、ステップ 8 固定 |
| 解像度 | 1 ステップの計算量と VAE デコードの VRAM | 短辺 768 が学習解像度。それ以下は情報量が減るだけで「劣化」ではなく「仕様」 | 試作は 480 級で固定。本番だけ上げる |
| pruned vs 非 pruned | — | 非 pruned Q3_K_M（15.6GB）vs pruned Q5_K_M（14.1GB）。「枝刈り」と「量子化」のどちらが H3 の質を削るか | RAM 32GB で両方回る。1 回だけ比較して方向を決める |

### 6.3 Phase 3 の成果物

次の 2 つの構成を決めて、それぞれ API JSON として Export する。

| プロファイル | 決め方 |
|---|---|
| **試作** | 予算 ≤ 5 分/カットの中で、動き量が FLOOR より落ちない最も質の高い点。おそらく Turbo 8step × 480 級 × Q4〜Q5 |
| **本番** | 予算 ≤ 15〜20 分/カットの中で、CEILING に最も近い点。おそらく Turbo 8〜12step × 544〜768 × w6a8〜int8 |

「本番でもベースのステップ数は使わない」という結論になる可能性が高い。それでよい。CEILING との差を数字で持っておくことが目的で、差が小さければ安心して使えるし、差が大きければ GPU 交換の根拠になる。

## 7. Phase 4 — 個別チューニング（決めた構成の上で詰める）

ここからは**構成ごとに閾値を詰める**。試作と本番で最適値が違う前提で、それぞれの構成で測る。**REF は各プロファイルの Phase 3 確定点**（CEILING ではない）。

| # | 施策 | 設定 | 注意 |
|---|---|---|---|
| D1 | EasyCache / LazyCache | ComfyUI 本体ノード。`reuse_threshold` を小さい値から上げ、動き量と PSNR が崩れる手前で止める。H3 の音声/映像の二出力に対応 | TeaCache の後継で H3 対応はこれだけ。コミュニティ報告で約 25%。**Turbo 8step では飛ばせるステップが少ないので効きが小さい**。本番（ステップ多め）の方が効く |
| D2 | Sol-Attn（スパース） | ComfyUI-sol-attn の `MiniMax H3 Scheduled Sol Attention Patch`。tau を低め（密）から。順序は KJ Sage → KJ H3 Sage → Sol の順が必須 | v0.6.2 で SM86 対応、3090 Ti で動作確認済み、**SM86 の性能は未計測**。作者の 5090 計測では Sage 比 1.14〜1.44 倍。**D1 の閾値と tau を同時に上げない**（作者も明記） |
| D3 | Turbo の強度とステップ | LoRA 強度 1.0 → 0.8、8step → 10〜12step | 動きが死ぬ症状の緩和。本番プロファイルで試す |
| D4 | int8 動画 VAE | `minimax_h3_video_vae_int8_convrot` | VAE デコードの VRAM を半分に。最終画質に直接触るので PSNR を厳しめに。本番解像度で OOM するなら A3 のタイル化と比較 |
| D5 | `--fast fp16_accumulation` | PyTorch 2.7 以降 | 累算を FP16 にする＝精度を落とす。**gpu-unlock が効いていれば旨味が減る**（制限されているのは FP32 累算の経路）。差が無ければ外す |
| D6 | Spectrum ノード | `ComfyUI-Spectrum-MiniMax-H3` | 報告が −2 倍〜+2 倍まで割れている近似加速。入れるなら同 seed で必ず A/B |
| D7 | FastH3 | 試作プロファイルの代替候補。`fastvideo_fasth3_8step_v2_pruned_int8_convrot` + `BlockSparseAttention`（VSA keep 10%） | 公式ドキュメントが「動きと音声の忠実度を速度と交換」と明記。Turbo 8step との比較で試作用に採用するか決める |

Studio には **試作と本番の 2 本**を登録し、コマ割りの確認は試作、承認する完成版は本番で撮り直す運用にする。Studio は読み込んだワークフローの設定を保持するので、JSON を切り替えるだけで済む。

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
| A6 Sage（Ampere） | 要計測 | FP8 経路が無い |
| A7 gpu-unlock | 計算部は最大 2 倍、全体は要計測 | 本家 README の 3060 値は BF16 27→51 TFLOPS。動画生成の実測は +15〜20%（Wan 2.2）。Turbo でステップが少ないほど効く |
| Turbo 8step（塩梅の軸） | サンプラー部で ×5 前後 | ステップ数比 |
| D1 EasyCache | 〜25%（ステップ多めの構成で） | コミュニティ報告 |
| D2 Sol-Attn | Sage 比 1.1〜1.4 倍（5090 値） | SM86 は未計測 |

**GPU 交換・RAM 増設の判断**は Phase 3 が終わった時点で行う:

- 試作プロファイルが予算内で回り、本番プロファイルの CEILING との差が許容範囲 → 現状維持
- 本番プロファイルで int8_convrot が予算に入らず、w6a8 / Q5 と CEILING の差が気になる → **まず RAM を 64GB に**（DDR4 なら安い）。int8 テキストエンコーダと重い量子化の DiT を RAM に置けるようになり、ディスクからの読み直しが消える
- 不満が「解像度・尺・Ref2VA の質」で VRAM が根本原因 → GPU 交換。その場合もここで作った計測プロトコルと試作 / 本番の 2 プロファイルはそのまま使える

## 10. 参考

- gpu-unlock: https://github.com/Zagitta/gpu-unlock（Linux 専用。Windows 移植は別プロジェクトで実験段階）
- Comfy-Org H3 重み一覧: https://huggingface.co/Comfy-Org/MiniMax-H3
- ComfyUI H3 ガイド: https://docs.comfy.org/tutorials/video/minimax/minimax-h3
- FastH3: https://docs.comfy.org/tutorials/video/minimax/minimax-h3-fastvideo
- ComfyUI-sol-attn（Chunk FF / Fused Modulation / Sol-Attn、SM86 対応）: https://github.com/Saganaki22/ComfyUI-sol-attn
- SageAttention: https://github.com/thu-ml/SageAttention
- pruned GGUF: https://huggingface.co/Abiray/MiniMax-H3-Pruned-GGUF
- 3060 12GB の実績スレッド: https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/35 / https://huggingface.co/Comfy-Org/MiniMax-H3/discussions/41
