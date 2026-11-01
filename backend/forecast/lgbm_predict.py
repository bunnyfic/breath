"""Pure-Python LightGBM predictor (no numpy / lightgbm needed on Lambda).

Reads the text model files written by Booster.save_model() and walks the trees.
Supports numerical splits (with missing-value handling) and categorical splits.
"""
import math


class Tree:
    __slots__ = ("num_leaves", "leaf_value", "split_feature", "threshold",
                 "decision_type", "left", "right", "cat_boundaries", "cat_threshold")


def _floats(s):
    return [float(v) for v in s.split()]


def _ints(s):
    return [int(v) for v in s.split()]


def load_model(path):
    trees, current = [], None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Tree="):
                current = {}
                trees.append(current)
                continue
            if line.startswith("end of trees"):
                break
            if current is not None and "=" in line:
                key, _, value = line.partition("=")
                current[key] = value

    out = []
    for d in trees:
        t = Tree()
        t.num_leaves = int(d["num_leaves"])
        t.leaf_value = _floats(d["leaf_value"])
        if t.num_leaves > 1:
            t.split_feature = _ints(d["split_feature"])
            t.threshold = _floats(d["threshold"])
            t.decision_type = _ints(d["decision_type"])
            t.left = _ints(d["left_child"])
            t.right = _ints(d["right_child"])
            t.cat_boundaries = _ints(d["cat_boundaries"]) if "cat_boundaries" in d else []
            t.cat_threshold = _ints(d["cat_threshold"]) if "cat_threshold" in d else []
        out.append(t)
    return out


def _predict_tree(t, x):
    if t.num_leaves == 1:
        return t.leaf_value[0]
    node = 0
    while node >= 0:
        fval = x[t.split_feature[node]]
        dt = t.decision_type[node]
        missing_type = (dt >> 2) & 3  # 0 none, 1 zero, 2 nan
        if dt & 1:  # categorical split
            if fval != fval:  # NaN
                if missing_type == 2:
                    node = t.right[node]
                    continue
                iv = 0
            else:
                iv = int(fval)
            if iv < 0:
                node = t.right[node]
                continue
            cat_idx = int(t.threshold[node])
            lo, hi = t.cat_boundaries[cat_idx], t.cat_boundaries[cat_idx + 1]
            word = iv >> 5
            in_set = word < (hi - lo) and (t.cat_threshold[lo + word] >> (iv & 31)) & 1
            node = t.left[node] if in_set else t.right[node]
        else:  # numerical split
            if fval != fval and missing_type != 2:
                fval = 0.0
            if (missing_type == 1 and abs(fval) <= 1e-35) or (missing_type == 2 and fval != fval):
                node = t.left[node] if dt & 2 else t.right[node]
            else:
                node = t.left[node] if fval <= t.threshold[node] else t.right[node]
    return t.leaf_value[~node]


def predict(trees, x):
    return sum(_predict_tree(t, x) for t in trees)