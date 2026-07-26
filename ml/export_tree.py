"""Export a scikit-learn decision tree to dependency-free C99.

``micromlgen`` and ``m2cgen`` both do this, but neither is reliably installable
offline and both emit code that is awkward to audit. The tree structure we need
is small and fully exposed by ``sklearn``'s ``tree_`` attribute, so we generate
the C directly. This keeps the firmware free of any runtime ML library and
makes the generated source readable enough to review by hand.

The emitted header exposes::

    int fire_model_predict(const float *features);
    const char *fire_model_label(int class_index);

with ``FIRE_MODEL_N_FEATURES`` and ``FIRE_MODEL_N_CLASSES`` as compile-time
constants.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from sklearn.tree import DecisionTreeClassifier

#: sklearn marks leaves with this sentinel in ``children_left``.
_LEAF = -1


def _emit_node(
    tree: object,
    node: int,
    depth: int,
    feature_names: Sequence[str],
    lines: list[str],
) -> None:
    """Recursively emit nested if/else for one subtree."""
    indent = "    " * (depth + 1)
    children_left = tree.children_left  # type: ignore[attr-defined]
    children_right = tree.children_right  # type: ignore[attr-defined]
    feature = tree.feature  # type: ignore[attr-defined]
    threshold = tree.threshold  # type: ignore[attr-defined]
    value = tree.value  # type: ignore[attr-defined]

    if children_left[node] == _LEAF:
        class_index = int(np.argmax(value[node][0]))
        counts = ", ".join(f"{v:.0f}" for v in value[node][0])
        lines.append(f"{indent}return {class_index}; /* samples [{counts}] */")
        return

    feature_index = int(feature[node])
    name = feature_names[feature_index]
    # sklearn's split rule is `X[feature] <= threshold` -> left child.
    lines.append(
        f"{indent}if (features[{feature_index}] <= {threshold[node]:.17g}) "
        f"{{ /* {name} */"
    )
    _emit_node(tree, children_left[node], depth + 1, feature_names, lines)
    lines.append(f"{indent}}} else {{")
    _emit_node(tree, children_right[node], depth + 1, feature_names, lines)
    lines.append(f"{indent}}}")


def export_decision_tree_to_c(
    model: DecisionTreeClassifier,
    feature_names: Sequence[str],
    class_names: Sequence[str],
) -> str:
    """Render ``model`` as a self-contained C99 header."""
    tree = model.tree_
    n_features = len(feature_names)
    if tree.n_features != n_features:
        raise ValueError(
            f"model expects {tree.n_features} features, got {n_features} names"
        )

    body: list[str] = []
    _emit_node(tree, 0, 0, feature_names, body)

    feature_doc = "\n".join(
        f" *   [{i}] {name}" for i, name in enumerate(feature_names)
    )
    label_entries = ",\n    ".join(f'"{name}"' for name in class_names)

    return f"""/*
 * fire_model.h - on-device fire classifier for FireProtect.
 *
 * GENERATED FILE - DO NOT EDIT BY HAND.
 * Regenerate with: python ml/train.py
 *
 * Source model: sklearn DecisionTreeClassifier
 *   max_depth      = {model.max_depth}
 *   node_count     = {tree.node_count}
 *   n_leaves       = {model.get_n_leaves()}
 *
 * Feature vector order (must match ml/generate_dataset.py FEATURE_COLUMNS and
 * the backend inference service):
{feature_doc}
 */

#ifndef FIRE_MODEL_H
#define FIRE_MODEL_H

#ifdef __cplusplus
extern "C" {{
#endif

#define FIRE_MODEL_N_FEATURES {n_features}
#define FIRE_MODEL_N_CLASSES {len(class_names)}

/* Class indices, ordered by severity. */
#define FIRE_MODEL_SAFE 0
#define FIRE_MODEL_WARNING 1
#define FIRE_MODEL_FIRE 2

static const char *const FIRE_MODEL_LABELS[FIRE_MODEL_N_CLASSES] = {{
    {label_entries}
}};

/*
 * Feature values and split thresholds are `double`, not `float`, on purpose.
 * scikit-learn evaluates `X[f] <= threshold` in float64. Narrowing to float32
 * moves samples that sit within one ULP of a threshold across the split and
 * breaks exact parity with the Python model (measured: 2 disagreements per
 * ~6900 held-out samples). The tree is only {model.max_depth} levels deep, so
 * even with software float64 on the ESP32 the cost is a handful of
 * microseconds per inference - far cheaper than a wrong classification.
 */
typedef double fire_feature_t;

/**
 * Classify one feature vector.
 *
 * @param features array of FIRE_MODEL_N_FEATURES values in the documented order
 * @return class index in [0, FIRE_MODEL_N_CLASSES)
 */
static inline int fire_model_predict(const fire_feature_t *features) {{
{chr(10).join(body)}
}}

/** Human-readable label for a class index, or "UNKNOWN" if out of range. */
static inline const char *fire_model_label(int class_index) {{
    if (class_index < 0 || class_index >= FIRE_MODEL_N_CLASSES) {{
        return "UNKNOWN";
    }}
    return FIRE_MODEL_LABELS[class_index];
}}

#ifdef __cplusplus
}}
#endif

#endif /* FIRE_MODEL_H */
"""
