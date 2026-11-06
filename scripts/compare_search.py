"""검색 시험 전·후 비교: 고치기 전 카드와 지금 카드로 같은 시험 질문을 돌려 새로 틀린 질문·새로 맞은 질문을 센다.

- 고치기 전: 이 갈래가 main 과 갈라진 곳(git merge-base)의 knowledge/ 와 web/engine.js 를 임시 폴더에 꺼내 돌린다.
  작업 폴더는 건드리지 않는다. 같은 갈래에서 finish 를 다시 돌려도 기준이 바뀌지 않는다.
- 시험 질문은 지금 것(_devset*.json, holdout.json)을 양쪽에 똑같이 쓴다. 고치기 전에 없던 질문은 '이번에 더한 시험 질문'으로 따로 센다.
- 시험 질문 글은 보고서와 화면에 적지 않는다(시험지를 지키려고). 기대 카드와 전·후 결과만 적는다.
- 비교를 못 해도(기준을 못 찾음 등) 종료 코드는 0 이다. 새 판 준비를 멈추지 않는다.

사용: python3 scripts/compare_search.py [--base <커밋>] [--out <보고서>]   → reports/search_compare.md
"""
import argparse
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "search_compare.md"


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def find_base():
    """이 갈래가 main 과 갈라진 곳(원격 main 먼저, 없으면 로컬 main). 못 찾으면 None"""
    for ref in ("origin/main", "main"):
        out = git("merge-base", "HEAD", ref)
        if out:
            return out.decode().strip()
    return None


def test_files(root):
    """시험 질문 파일(분야별 _devset*.json, holdout.json)의 root 기준 경로"""
    k = root / "knowledge"
    files = [p.relative_to(root) for p in k.glob("*/_devset*.json")]
    if (k / "holdout.json").exists():
        files.append(Path("knowledge") / "holdout.json")
    return sorted(files)


def questions(root):
    """시험 질문 묶음 → {(묶음, 질문)}"""
    qs = set()
    for rel in test_files(root):
        kind = "holdout" if rel.name == "holdout.json" else "dev"
        qs |= {(kind, it["q"]) for it in json.loads((root / rel).read_text(encoding="utf-8")).get("items", [])}
    return qs


def prepare_before(base, td):
    """기준 시점의 카드·엔진을 td 에 꺼내고 시험 질문과 시험 프로그램은 지금 것으로 바꾼다 → 그때 있던 시험 질문"""
    tar = git("archive", base, "knowledge", "web/engine.js")
    if tar is None:
        raise RuntimeError(f"기준 {base[:7]} 의 카드를 꺼내지 못함")
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(td, filter="data")
        else:
            t.extractall(td)
    old = questions(td)
    for rel in test_files(td):
        (td / rel).unlink()
    for rel in test_files(ROOT):
        (td / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, td / rel)
    (td / "scripts").mkdir(exist_ok=True)
    for name in ("test_engine.js", "load_cards.js"):
        shutil.copyfile(ROOT / "scripts" / name, td / "scripts" / name)
    return old


def start(root, out):
    return subprocess.Popen(["node", str(root / "scripts" / "test_engine.js"), "--rows", str(out)], cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8")


def finish_run(proc, out, what):
    err = proc.communicate()[1]
    if proc.returncode != 0 or not out.exists():
        tail = " / ".join((err or "").strip().splitlines()[-2:])
        raise RuntimeError(f"{what} 시험을 돌리지 못함({tail})")
    return json.loads(out.read_text(encoding="utf-8"))


def keyed(items):
    """질문별 결과 → {(묶음, 질문, 몇 번째): 결과}. 같은 질문이 둘 있으면 차례로 구별한다"""
    seen, out = {}, {}
    for it in items:
        k = (it["set"], it["q"])
        seen[k] = seen.get(k, 0) + 1
        out[k + (seen[k],)] = it
    return out


def compare(before, after, old_questions):
    """전·후 질문별 결과 비교 → {"common", "fixed", "broke", "added"} (결과 쌍 목록)"""
    b, a = keyed(before), keyed(after)
    res = {"common": [], "fixed": [], "broke": [], "added": []}
    for k, now in a.items():
        was = b.get(k)
        if was is None or (k[0], k[1]) not in old_questions:
            res["added"].append((None, now))
            continue
        res["common"].append((was, now))
        if was["ok"] and not now["ok"]:
            res["broke"].append((was, now))
        elif not was["ok"] and now["ok"]:
            res["fixed"].append((was, now))
    return res


def said(it):
    if it["got"] == "ask":
        return "되묻기(" + ", ".join(it["ids"][:3]) + ")"
    if it["got"] == "fallback":
        return "조문 검색"
    return it["got"] + " 로 바로 답"


def mark(it):
    return "맞음" if it["ok"] else ("엉뚱한 답" if it["wrong"] else "틀림")


def card_titles():
    titles = {}
    for f in (ROOT / "knowledge").glob("*/[!_]*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, list):
            titles.update({c["id"]: c.get("title", "") for c in data if isinstance(c, dict) and "id" in c})
    return titles


def line(was, now, titles):
    want = ", ".join(f"{e}({titles[e]})" if titles.get(e) else e for e in now["expect"])
    before = f"고치기 전 {said(was)}({mark(was)}) → " if was else ""
    return f"- 기대 {want}: {before}고친 뒤 {said(now)}({mark(now)})"


def report(base, res, loo_b, loo_a, titles):
    common = res["common"]
    ok_b, ok_a = sum(w["ok"] for w, _ in common), sum(n["ok"] for _, n in common)
    wr_b, wr_a = sum(w["wrong"] for w, _ in common), sum(n["wrong"] for _, n in common)
    lines = ["# 검색 시험 전·후 비교", "",
             f"고치기 전: {base[:7]}(이 갈래가 main 과 갈라진 곳) · 고친 뒤: 지금 작업 폴더",
             "시험 질문 글은 시험지를 지키려고 적지 않는다. 기대 카드와 결과만 적는다.", "",
             "| | 고치기 전 | 고친 뒤 |", "|---|---|---|",
             f"| 시험 질문 맞게 처리(전·후 모두 있는 {len(common)}개) | {ok_b} | {ok_a} |",
             f"| 엉뚱한 답을 바로 냄 | {wr_b} | {wr_a} |",
             f"| 예상 질문 빼고 찾기 1위 정답 | {loo_b['top1']} ({loo_b['n']}개) | {loo_a['top1']} ({loo_a['n']}개) |",
             f"| 예상 질문 빼고 찾기 3위 안 | {loo_b['top3']} | {loo_a['top3']} |", ""]
    for key, title, note in (("broke", "새로 틀린 질문", "고친 뒤 틀리게 된 질문. 기대 카드를 새 원문대로 다시 본다(시험 질문 글을 찾아 카드에 옮기지 않는다)."),
                             ("fixed", "새로 맞은 질문", ""),
                             ("added", "이번에 더한 시험 질문", "고치기 전에는 없던 질문(새 내규 등).")):
        lines.append(f"## {title} ({len(res[key])}개)")
        if note:
            lines.append(note)
        lines += [line(w, n, titles) for w, n in res[key]] + [""]
    return "\n".join(lines), (ok_b, ok_a, wr_b, wr_a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="고치기 전으로 볼 커밋(기본: main 과 갈라진 곳)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.out)
    try:
        base = args.base or find_base()
        if not base:
            raise RuntimeError("고치기 전 기준(main)을 찾지 못함")
        full = git("rev-parse", base)
        if not full:
            raise RuntimeError(f"기준 커밋 {base} 이 없음")
        base = full.decode().strip()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            old_q = prepare_before(base, td)
            pb, pa = start(td, td / "rows_before.json"), start(ROOT, td / "rows_after.json")
            before = finish_run(pb, td / "rows_before.json", "고치기 전")
            after = finish_run(pa, td / "rows_after.json", "고친 뒤")
    except (RuntimeError, OSError, ValueError) as e:
        out.write_text(f"# 검색 시험 전·후 비교\n\n비교하지 못함: {e}\n", encoding="utf-8")
        print(f"경고: 검색 시험 전·후 비교를 하지 못했습니다({e}). 새 판 준비에는 영향이 없습니다")
        return
    res = compare(before["items"], after["items"], old_q)
    text, (ok_b, ok_a, wr_b, wr_a) = report(base, res, before["loo"], after["loo"], card_titles())
    out.write_text(text, encoding="utf-8")
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"검색 시험 비교(고치기 전 {base[:7]}): 맞게 처리 {ok_b} → {ok_a} (질문 {len(res['common'])}개), 엉뚱한 답 {wr_b} → {wr_a}, "
          f"새로 틀림 {len(res['broke'])}개, 새로 맞음 {len(res['fixed'])}개, 이번에 더한 질문 {len(res['added'])}개 → {shown}")
    if res["broke"]:
        print(f"경고: 고친 뒤 새로 틀린 시험 질문 {len(res['broke'])}개 — {shown} 의 기대 카드를 새 원문대로 다시 보세요"
              "(시험 질문 글을 찾아 카드에 옮기지 마세요)")


if __name__ == "__main__":
    main()
