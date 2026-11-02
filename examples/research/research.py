"""Deterministic, CPU-only retrieval experiment; no network or dependencies."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

DOCS = {
    "d01": "research experiment records dataset configuration evaluation metrics reproducibility",
    "d02": "distributed git collaboration branches commits merge conflicts version control",
    "d03": "language agents context memory summary evidence progressive disclosure retrieval",
    "d04": "machine learning evaluation training validation held out test dataset leakage",
    "d05": "database transactions concurrency locks atomic writes consistency recovery",
    "d06": "information retrieval search ranking term frequency inverse document frequency bm25",
    "d07": "research research research scientific claims evidence replication limitations",
    "d08": "software tests failing regression verification code quality assertions",
    "d09": "long running agents interruption handoff state recovery unfinished work",
    "d10": "token cost latency budget caching context compression efficiency",
    "d11": "data privacy access permissions authentication credentials security",
    "d12": "research research notes project summaries instructions source references version",
}
QUERIES = {
    "dev": [("experiment dataset metrics", "d01"), ("git merge branches", "d02"),
            ("context evidence memory", "d03"), ("test dataset leakage", "d04"),
            ("atomic concurrency recovery", "d05"), ("research scientific evidence", "d07")],
    "holdout": [("search term ranking", "d06"), ("failing code verification", "d08"),
                ("agents unfinished recovery", "d09"), ("token compression cost", "d10"),
                ("access credentials privacy", "d11"), ("research project source version", "d12")],
}


def evaluate(split):
    words = lambda x: re.findall(r"[a-z]+", x.lower())
    counts = {name: Counter(words(doc)) for name, doc in DOCS.items()}
    avg_len = sum(sum(c.values()) for c in counts.values()) / len(counts)
    config = {"methods": ["binary_overlap", "bm25"], "bm25": {"k1": 1.2, "b": 0.75}, "top_k": 3, "split": split}
    sha = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()
    output = {"config": config, "collection_sha256": sha(DOCS), "queries_sha256": sha(QUERIES[split]),
              "configuration_sha256": sha(config), "query_count": len(QUERIES[split]), "results": {}}
    for method in config["methods"]:
        rows = []
        for query, gold in QUERIES[split]:
            scores = {}
            for name, c in counts.items():
                score = 0.0
                for term in set(words(query)):
                    if method == "binary_overlap":
                        score += float(term in c)
                    else:
                        df = sum(term in other for other in counts.values())
                        idf = math.log(1 + (len(counts)-df+0.5)/(df+0.5))
                        tf = c[term]
                        score += idf*tf*2.2/(tf+1.2*(0.25+0.75*sum(c.values())/avg_len))
                scores[name] = score
            ranking = sorted(scores, key=lambda name: (-scores[name], name))[:3]
            rows.append({"query": query, "gold": gold, "ranking": ranking,
                         "recall_at_1": int(ranking[0] == gold), "rr_at_3": 1/(ranking.index(gold)+1) if gold in ranking else 0})
        output["results"][method] = {"recall_at_1": sum(x["recall_at_1"] for x in rows)/len(rows),
                                     "mrr_at_3": sum(x["rr_at_3"] for x in rows)/len(rows), "queries": rows}
    return output


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=QUERIES, required=True)
    p.add_argument("--output"); p.add_argument("--verify")
    args = p.parse_args()
    result = evaluate(args.split)
    if args.verify:
        assert result == json.loads(Path(args.verify).read_text()), "Stored result differs from recomputation"
        print("Verified exact reproduction")
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({k: {m: v for m,v in row.items() if m != "queries"} for k,row in result["results"].items()}))
