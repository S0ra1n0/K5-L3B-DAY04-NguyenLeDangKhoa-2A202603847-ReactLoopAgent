"""Chấm lại câu trả lời ĐÃ CÓ trong một file eval bằng judge khác; không chạy lại agent.

Dùng để so sánh judge một cách công bằng: cùng câu trả lời của agent, chỉ đổi cách chấm.
Dùng nguyên grade_case/summarize của lab (không sửa grading.py). File output được ghi sau
mỗi case, nên nếu bị ngắt có thể chạy lại cùng lệnh để chấm tiếp các case còn thiếu.

Ví dụ:
  python3 evals/rejudge.py --input eval_before.json --output eval_before_rejudged.json \
      --judge-provider ollama --judge-model qwen3.5:9b --judge-temperature 0
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from providers import Provider
from grading import grade_case, summarize, bonus_points

JUDGE_KEYS = ('judge_provider', 'judge_model', 'judge_reasoning_effort', 'judge_temperature')


def main():
    parser = argparse.ArgumentParser(description='Chấm lại file eval có sẵn bằng judge khác')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--judge-provider', required=True)
    parser.add_argument('--judge-model', required=True)
    parser.add_argument('--judge-reasoning-effort', default=None, help="Ví dụ 'none'; bỏ trống = mặc định của model")
    parser.add_argument('--judge-temperature', type=float, default=None)
    args = parser.parse_args()

    source = json.loads(Path(args.input).read_text(encoding='utf-8'))
    dataset = source['config'].get('dataset', 'core')
    cases_path = ROOT / ('bonus/cases.json' if dataset == 'bonus' else 'evals/cases.json')
    case_defs = {c['id']: c for c in json.loads(cases_path.read_text(encoding='utf-8'))}

    config = dict(source['config'])
    for key in JUDGE_KEYS:
        config[key] = getattr(args, key)
    config['rejudged_from'] = Path(args.input).name

    done = {}
    out_path = Path(args.output)
    if out_path.exists():
        previous = json.loads(out_path.read_text(encoding='utf-8'))
        if previous.get('config') == config:
            done = {c['id']: c for c in previous['cases'] if c['grade']['status'] == 'scored'}
            print(f'Tiếp tục: đã có {len(done)} case được chấm trong {out_path.name}', flush=True)

    judge = Provider(args.judge_provider, args.judge_model,
                     reasoning_effort=args.judge_reasoning_effort, temperature=args.judge_temperature)
    cases = []
    for item in source['cases']:
        if item['id'] in done:
            cases.append(done[item['id']])
            continue
        print(f"Đang chấm lại {item['id']}", flush=True)
        try:
            grade = grade_case(case_defs[item['id']], item['result'], judge)
        except Exception as exc:  # giống run_evals: lỗi hệ thống thì unscored, không coi là 0
            grade = {'score': None, 'status': 'unscored', 'reason': str(exc), 'judge_usage': {}}
        new_item = dict(item)
        for key in ('judge_provider', 'judge_model'):
            new_item[key] = getattr(args, key)
        new_item['grade'] = grade
        new_item['previous_grade'] = {k: item['grade'].get(k) for k in ('score', 'status', 'dimensions')}
        cases.append(new_item)
        print(f"{item['id']}: {item['grade'].get('score')} -> {grade.get('score')} ({grade['status']})", flush=True)
        write(out_path, source, config, cases + source['cases'][len(cases):], dataset, partial=True)
    report = write(out_path, source, config, cases, dataset, partial=False)
    summary = report['summary']
    print(f"Điểm: {source['summary'].get('score_0_100')} -> {summary['score_0_100']}/100; "
          f"đã chấm {summary['scored_cases']}/{summary['total_cases']} case. File: {out_path}")
    return 0 if summary['score_0_100'] is not None else 1


def write(path, source, config, cases, dataset, partial):
    """Ghi file; khi chưa xong, các case chưa chấm lại được đánh dấu unscored để summary không lẫn judge cũ."""
    if partial:
        cases = [c if 'previous_grade' in c else {**c, 'grade': {'score': None, 'status': 'unscored', 'reason': 'Chưa chấm lại.'}}
                 for c in cases]
    report = {'summary': summarize(cases), 'config': config, 'cases': cases}
    if dataset == 'bonus':
        report['summary']['bonus_points_0_15'] = bonus_points(report['summary']['score_0_100'], True)
    if source.get('reruns'):
        report['reruns'] = source['reruns']  # câu trả lời của agent ở các case này đến từ lần chạy lại
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    sys.exit(main())
