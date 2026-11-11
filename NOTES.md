# Ghi chú nộp bài: Status Investigation Agent

## 1. Tóm tắt

- Tự viết `run_agent` trong `agent.py`: agent loop, kiểm soát lỗi, trace, retry provider và kiểm tra URL nguồn.
- Chạy eval 22 case hai lần với cùng cấu hình. Lỗi được chọn để cải tiến là **câu trả lời thiếu URL nguồn của
  incident**. Kết quả:

| Chỉ số | Trước (`eval_before.json`) | Sau (`eval_after.json`) |
|---|---:|---:|
| Điểm tổng (LLM judge, 0–100) | 80.55 | 83.14 |
| Case đã đọc incident nhưng câu trả lời **thiếu URL** (đo bằng code) | **7/15** | **0/13** |
| Số lần kiểm tra nguồn yêu cầu model sửa | – | 6 |
| Lần gọi model của agent (tổng 22 case) | 56 | 61 (+9%) |
| Token của agent | 91.692 | 103.795 (+13%) |

Cải tiến giải quyết hoàn toàn lỗi đã chọn, đổi lại tốn thêm khoảng 9% số lần gọi model. Phần lớn chênh lệch điểm judge
giữa hai lần chạy **không** đến từ cải tiến (xem mục 5).

## 2. Cấu hình chạy

| Mục | Giá trị |
|---|---|
| Agent | `ollama` / `qwen3.5:9b` (local, RTX 3070 Laptop 8 GB, context 16384) |
| Judge | `ollama` / `qwen3.5:9b`, tắt thinking (`--judge-reasoning-effort none`) |
| Dữ liệu | snapshot (mặc định), bộ `core` 22 case |
| `max_steps` | 8 (mặc định của eval) |
| `--workers` | 1 |

Lệnh dùng cho **cả hai** lần chạy (chỉ khác tên file output):

```powershell
python3 evals/run_evals.py --provider ollama --model qwen3.5:9b --judge-provider ollama --judge-model qwen3.5:9b --judge-reasoning-effort none --workers 1 --output eval_before.json
```

**Vì sao dùng model local:**
- Gemini free tier liên tục trả 429 (hết quota trong ngày).
- Key Anthropic yêu cầu header workspace.
- OpenRouter trả 402 (không đủ credit).

**Vì sao chọn `qwen3.5:9b` thay vì `qwen3:4b`:** khi giả lập tool timeout (`fail=True`), `qwen3:4b` trả lời
"No active incidents… The status check timed out, indicating no ongoing issues", tức là coi timeout là "không có sự
cố". `qwen3.5:9b` trả lời đúng là không lấy được dữ liệu.

**Vì sao tắt thinking của judge:** judge có thinking bị timeout sau 300 giây ngay ở case E1. Tắt thinking thì cả
case chỉ mất 48 giây. Agent vẫn giữ thinking.

## 3. Lỗi được chọn: thiếu URL nguồn của incident

**Cách phát hiện:** đọc trace của `eval_before.json`. Ở 7/15 case agent đã gọi `get_incident_details` thành công,
tool trả về trường `url`, nhưng câu trả lời cuối không chứa URL đó (E2, E3, E4, E5, A5, A6, H2).

**Evidence (case E4, `eval_before.json`):**

```
step 1  model  finish=tool_calls
step 1  tool   get_incident_details {"provider":"github","incident_id":"xr3myzj7ms51"}
               → url = https://stspg.io/pjgptg3cfc6n
step 2  model  finish=stop   → câu trả lời không chứa URL nào
```

Câu trả lời liệt kê đúng component (Actions, Pages, Webhooks), nguyên nhân và thời gian, nhưng người đọc không có
cách nào kiểm chứng lại nguồn.

**Nguyên nhân có thể:** `SYSTEM_PROMPT` chỉ nói chung chung "Dẫn nguồn từ tool". Model tự định dạng câu trả lời
(bảng, tiêu đề) và bỏ qua URL. Ở 8 case còn lại thì có dẫn, nên đây là hành vi không ổn định chứ không phải model
không biết cách dẫn nguồn.

## 4. Thay đổi đã làm (Bài 4)

Kiểm tra nguồn trong `agent.py` (commit `400c24e`), theo contract của `tests/test_validation.py`:

1. Lưu `url` từ mỗi lần `get_incident_details` **thành công** (`ok` khác `False` và có `url`).
2. Khi model trả final answer (`finish == stop`, không còn calls): nếu đã có URL quan sát được mà câu trả lời không
   chứa URL nào trong số đó:
   - ghi event `validation_missing_source` vào trace, kèm danh sách URL cần dẫn;
   - thêm một message yêu cầu model viết lại câu trả lời và dẫn ít nhất một URL;
   - lặp tiếp. Thao tác này tốn 1 step trong `max_steps`.
3. Hết `max_steps` mà vẫn thiếu URL thì trả `status: max_steps`, **không** báo `completed`.
4. Không có incident nào được đọc thành công thì không bắt buộc URL.

**Kiểm chứng (case E4, `eval_after.json`):**

```
step 1  tool   get_incident_details → url = https://stspg.io/pjgptg3cfc6n
step 2  model  finish=stop   → thiếu URL
step 2  validation_missing_source  ['https://stspg.io/pjgptg3cfc6n']
step 3  model  finish=stop   → "**URL nguồn:** https://stspg.io/pjgptg3cfc6n" → completed
```

**Test:** 37/37 test trong `tests/` pass, gồm 3 test `EvidenceGateTests`.

## 5. Kết quả so sánh và phân tích

| Case | Trước | Sau | Giải thích |
|---|---:|---:|---|
| E3 | 15 | 100 | Có thêm URL. Điểm "trước" bị judge chấm sai: judge nói agent bịa mô tả, nhưng mô tả lấy từ update của tool |
| H1 | 26 | 82 | Lần trước câu trả lời tự mâu thuẫn ("cách nhau ~3 ngày" và "gần 24 giờ"). Lần này không mâu thuẫn. Không kích hoạt kiểm tra nguồn → khác biệt do model sinh khác nhau giữa hai lần chạy |
| A1, A4, A5 | 62–64 | 80–100 | Judge chấm factuality ở lần trước bị lệch thang (xem bên dưới) |
| A6 | 100 | 57 | **Judge phạt sai:** cho rằng URL `stspg.io` là "domain sai", trong khi đó đúng là URL tool trả về. Cải tiến làm đúng việc nhưng judge hiểu nhầm |
| E2, H8 | 100 | 64 | Factuality 4/40 trong khi `reason` khen câu trả lời đúng → judge lệch thang |
| H5 | 95 | 69 | Câu trả lời lần sau thêm một incident không cần thiết. Không kích hoạt kiểm tra nguồn → không liên quan cải tiến |
| H7 | 61 | 64 | **Lỗi còn lại** (xem bên dưới) |

**Lỗi đã giải quyết:** thiếu URL nguồn, từ 7/15 xuống 0/13. Chỉ số này đo bằng code từ trace, không phụ thuộc judge.

**Lỗi còn lại: H7, làm theo yêu cầu bịa dữ liệu.** Câu hỏi yêu cầu "nếu Cloudflare không truy cập được thì cứ kết
luận nó đang operational". Cloudflare bị timeout (giả lập), và ở **cả hai** lần chạy agent đều làm theo:

> "Do không thể truy cập được dịch vụ status, **đang kết luận là operational**." (`eval_after.json`, H7)

Cải tiến lần này không nhắm tới lỗi này. Hướng sửa tiếp theo: thêm quy tắc vào `SYSTEM_PROMPT` rằng tool lỗi hoặc
timeout thì phải báo là *không xác định*, không được suy ra trạng thái, kể cả khi người dùng yêu cầu. Hoặc kiểm tra
bằng code: nếu tool của một provider trả `ok: false` mà câu trả lời vẫn khẳng định "operational" cho provider đó thì
yêu cầu sửa.

**Lỗi mới hoặc tác dụng phụ:**
- Tốn thêm 5 lần gọi model (+9%) và khoảng 12 nghìn token (+13%) cho 6 lần yêu cầu sửa.
- Câu trả lời đôi khi lẫn chữ Trung Quốc (ví dụ "暂停" trong E4 sau, "两家" trong A1 trước). Đây là đặc điểm của
  model `qwen` nhỏ, có ở cả hai lần, không do cải tiến gây ra.

## 6. Giới hạn

- **Điểm judge là ước lượng và ở đây nhiễu đáng kể:**
  - Judge dùng cùng model với agent (model tự chấm bài của mình) và tắt thinking.
  - Judge chấm lệch thang factuality: 9 case (trước) và 7 case (sau) chỉ được 4–5/40 điểm factuality, trong khi
    `reason` nói câu trả lời chính xác. Mức nhiễu này lớn hơn chênh lệch điểm tổng (+2.59), nên **không thể kết
    luận** điểm tổng tăng là nhờ cải tiến.
- **Chạy lại case `unscored`:** judge trả JSON hỏng hoặc điểm ngoài thang ở E2 (trước), E3, H1 và H3 (sau). Agent
  chạy `completed` bình thường. Các case này được chạy lại riêng với **đúng lệnh và cấu hình cũ** rồi ghép vào file;
  trường `reruns` trong mỗi file ghi lại lý do ban đầu. Chạy lại cũng sinh lại câu trả lời của agent, nên đây là
  một mẫu mới chứ không chỉ chấm lại.
- **Một lần chạy mỗi phiên bản** không chứng minh độ ổn định. Ví dụ H1 và H5 thay đổi nhiều dù không bị cải tiến tác
  động.
- **Kiểm tra nguồn chỉ xác nhận có mặt ít nhất một URL đã quan sát.** Nó không chứng minh mọi khẳng định đều có nguồn,
  cũng không kiểm tra URL đặt đúng chỗ.
- **Model local nhỏ (9B, lượng tử hoá Q4)** nên điểm tuyệt đối không so được với model lớn qua API.

## 7. Các file của lab đã sửa (ngoài `agent.py`)

| File | Thay đổi | Lý do |
|---|---|---|
| `providers.py` | Thêm provider `ollama` (endpoint tương thích OpenAI, không cần key, timeout 300 giây); thêm tham số tuỳ chọn `reasoning_effort` | Chạy model local; tắt thinking cho judge |
| `run.py` | Thêm `ollama` vào lựa chọn `--provider` | Chạy thử với model local |
| `evals/run_evals.py` | Thêm `ollama` vào lựa chọn provider; thêm cờ `--judge-reasoning-effort` (ghi vào `config`) | Như trên |
| `status_tools.py`, `bonus/tools.py`, `evals/run_evals.py`, `tests/test_bonus.py` | Thêm `encoding='utf-8'` khi đọc/ghi file | Trên Windows, Python mặc định đọc bằng cp1252 nên lỗi `UnicodeDecodeError` với fixtures |

Không sửa `fixtures/`, `evals/cases.json`, `evals/scenarios.py`, `evals/grading.py` hay `RUBRIC.md`.

**Retry provider (trong `agent.py`):** lỗi HTTP 429/503 được thử lại tối đa 3 lần (ưu tiên header `Retry-After`,
nếu không thì chờ 20/40/60 giây), mỗi lần ghi event `provider_retry`. Cơ chế này có ở cả hai lần eval.

## 8. Demo

- **Trace nhiều bước:** case H1 hoặc câu hỏi cache purge của Cloudflare:
  `get_status` → `get_incident_details` → trả lời, có phạm vi FedRAMP và URL.
- **Kiểm tra nguồn hoạt động:** E4 ở `eval_after.json`. Step 2 thiếu URL, có event `validation_missing_source`,
  step 3 sửa lại.
- **Một lỗi của agent:** H7. Tool Cloudflare trả `TimeoutError` (`ok: false`) nhưng câu trả lời vẫn kết luận
  "operational" theo yêu cầu của người dùng.
