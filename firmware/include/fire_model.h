/*
 * fire_model.h - on-device fire classifier for FireProtect.
 *
 * GENERATED FILE - DO NOT EDIT BY HAND.
 * Regenerate with: python ml/train.py
 *
 * Source model: sklearn DecisionTreeClassifier
 *   max_depth      = 9
 *   node_count     = 137
 *   n_leaves       = 69
 *
 * Feature vector order (must match ml/generate_dataset.py FEATURE_COLUMNS and
 * the backend inference service):
 *   [0] temperature_c
 *   [1] humidity_pct
 *   [2] smoke_ppm
 *   [3] air_quality_ppm
 *   [4] flame_analog_volts
 *   [5] flame_detected
 *   [6] temp_rate_c_per_min
 *   [7] smoke_rate_ppm_per_min
 *   [8] heat_index_c
 */

#ifndef FIRE_MODEL_H
#define FIRE_MODEL_H

#ifdef __cplusplus
extern "C" {
#endif

#define FIRE_MODEL_N_FEATURES 9
#define FIRE_MODEL_N_CLASSES 3

/* Class indices, ordered by severity. */
#define FIRE_MODEL_SAFE 0
#define FIRE_MODEL_WARNING 1
#define FIRE_MODEL_FIRE 2

static const char *const FIRE_MODEL_LABELS[FIRE_MODEL_N_CLASSES] = {
    "SAFE",
    "WARNING",
    "FIRE"
};

/*
 * Feature values and split thresholds are `double`, not `float`, on purpose.
 * scikit-learn evaluates `X[f] <= threshold` in float64. Narrowing to float32
 * moves samples that sit within one ULP of a threshold across the split and
 * breaks exact parity with the Python model (measured: 2 disagreements per
 * ~6900 held-out samples). The tree is only 9 levels deep, so
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
static inline int fire_model_predict(const fire_feature_t *features) {
    if (features[2] <= 2200.0079345703125) { /* smoke_ppm */
        if (features[2] <= 724.09298706054688) { /* smoke_ppm */
            if (features[0] <= 25.15000057220459) { /* temperature_c */
                if (features[3] <= 116.4734992980957) { /* air_quality_ppm */
                    if (features[7] <= -1091.2650146484375) { /* smoke_rate_ppm_per_min */
                        if (features[0] <= 19.899999618530273) { /* temperature_c */
                            return 1; /* samples [0, 1, 0] */
                        } else {
                            return 0; /* samples [1, 0, 0] */
                        }
                    } else {
                        if (features[0] <= 24.949999809265137) { /* temperature_c */
                            if (features[3] <= 109.37699890136719) { /* air_quality_ppm */
                                if (features[1] <= 47.450000762939453) { /* humidity_pct */
                                    if (features[7] <= 702.91500854492188) { /* smoke_rate_ppm_per_min */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    if (features[2] <= 611.88998413085938) { /* smoke_ppm */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                }
                            } else {
                                if (features[8] <= 22.194000244140625) { /* heat_index_c */
                                    if (features[1] <= 47.149999618530273) { /* humidity_pct */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    if (features[8] <= 22.852499961853027) { /* heat_index_c */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                }
                            }
                        } else {
                            if (features[2] <= 607.5570068359375) { /* smoke_ppm */
                                if (features[1] <= 45.149999618530273) { /* humidity_pct */
                                    if (features[6] <= 4.5) { /* temp_rate_c_per_min */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    return 0; /* samples [1, 0, 0] */
                                }
                            } else {
                                if (features[7] <= 456.64500427246094) { /* smoke_rate_ppm_per_min */
                                    return 0; /* samples [1, 0, 0] */
                                } else {
                                    return 0; /* samples [1, 0, 0] */
                                }
                            }
                        }
                    }
                } else {
                    if (features[2] <= 565.9210205078125) { /* smoke_ppm */
                        if (features[2] <= 533.83847045898438) { /* smoke_ppm */
                            if (features[3] <= 142.56999969482422) { /* air_quality_ppm */
                                if (features[8] <= 22.300000190734863) { /* heat_index_c */
                                    if (features[7] <= -722.32501220703125) { /* smoke_rate_ppm_per_min */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    if (features[3] <= 124.30599975585938) { /* air_quality_ppm */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                }
                            } else {
                                if (features[2] <= 512.916015625) { /* smoke_ppm */
                                    return 0; /* samples [1, 0, 0] */
                                } else {
                                    if (features[8] <= 20.272500038146973) { /* heat_index_c */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                }
                            }
                        } else {
                            if (features[1] <= 48.350000381469727) { /* humidity_pct */
                                if (features[8] <= 23.260000228881836) { /* heat_index_c */
                                    if (features[8] <= 22.194000244140625) { /* heat_index_c */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                } else {
                                    if (features[0] <= 24.949999809265137) { /* temperature_c */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                }
                            } else {
                                return 0; /* samples [1, 0, 0] */
                            }
                        }
                    } else {
                        if (features[8] <= 20.330499649047852) { /* heat_index_c */
                            if (features[8] <= 19.199999809265137) { /* heat_index_c */
                                if (features[1] <= 49.149999618530273) { /* humidity_pct */
                                    if (features[3] <= 137.94349670410156) { /* air_quality_ppm */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                } else {
                                    return 0; /* samples [1, 0, 0] */
                                }
                            } else {
                                if (features[2] <= 624.24649047851562) { /* smoke_ppm */
                                    if (features[1] <= 47.25) { /* humidity_pct */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                } else {
                                    if (features[3] <= 139.43000030517578) { /* air_quality_ppm */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                }
                            }
                        } else {
                            if (features[3] <= 145.66750335693359) { /* air_quality_ppm */
                                if (features[1] <= 46.450000762939453) { /* humidity_pct */
                                    if (features[8] <= 21.490499496459961) { /* heat_index_c */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    if (features[1] <= 50.75) { /* humidity_pct */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                }
                            } else {
                                if (features[1] <= 55.799999237060547) { /* humidity_pct */
                                    if (features[2] <= 631.531005859375) { /* smoke_ppm */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                } else {
                                    return 0; /* samples [1, 0, 0] */
                                }
                            }
                        }
                    }
                }
            } else {
                if (features[4] <= 0.15030000358819962) { /* flame_analog_volts */
                    if (features[1] <= 49.600000381469727) { /* humidity_pct */
                        if (features[3] <= 149.57149505615234) { /* air_quality_ppm */
                            if (features[0] <= 27.449999809265137) { /* temperature_c */
                                if (features[1] <= 47.69999885559082) { /* humidity_pct */
                                    if (features[6] <= 1.5) { /* temp_rate_c_per_min */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                } else {
                                    return 0; /* samples [1, 0, 0] */
                                }
                            } else {
                                return 0; /* samples [1, 0, 0] */
                            }
                        } else {
                            if (features[8] <= 28.995499610900879) { /* heat_index_c */
                                if (features[3] <= 151.39199829101562) { /* air_quality_ppm */
                                    return 1; /* samples [0, 1, 0] */
                                } else {
                                    return 1; /* samples [0, 1, 0] */
                                }
                            } else {
                                return 1; /* samples [0, 1, 0] */
                            }
                        }
                    } else {
                        return 0; /* samples [1, 0, 0] */
                    }
                } else {
                    return 0; /* samples [1, 0, 0] */
                }
            }
        } else {
            if (features[0] <= 46.149999618530273) { /* temperature_c */
                if (features[1] <= 65.200000762939453) { /* humidity_pct */
                    if (features[5] <= 0.5) { /* flame_detected */
                        if (features[3] <= 121.052001953125) { /* air_quality_ppm */
                            if (features[8] <= 25.138999938964844) { /* heat_index_c */
                                if (features[1] <= 46.399999618530273) { /* humidity_pct */
                                    if (features[2] <= 795.50601196289062) { /* smoke_ppm */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    if (features[2] <= 765.41049194335938) { /* smoke_ppm */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                }
                            } else {
                                if (features[8] <= 26.663000106811523) { /* heat_index_c */
                                    return 1; /* samples [0, 1, 0] */
                                } else {
                                    return 1; /* samples [0, 1, 0] */
                                }
                            }
                        } else {
                            if (features[2] <= 860.70199584960938) { /* smoke_ppm */
                                if (features[4] <= 0.20109999924898148) { /* flame_analog_volts */
                                    if (features[3] <= 126.9265022277832) { /* air_quality_ppm */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                } else {
                                    if (features[1] <= 47.900001525878906) { /* humidity_pct */
                                        return 0; /* samples [1, 0, 0] */
                                    } else {
                                        return 1; /* samples [0, 1, 0] */
                                    }
                                }
                            } else {
                                if (features[2] <= 899.86849975585938) { /* smoke_ppm */
                                    if (features[4] <= 0.37429998815059662) { /* flame_analog_volts */
                                        return 1; /* samples [0, 1, 0] */
                                    } else {
                                        return 0; /* samples [1, 0, 0] */
                                    }
                                } else {
                                    return 1; /* samples [0, 1, 0] */
                                }
                            }
                        }
                    } else {
                        return 2; /* samples [0, 0, 1] */
                    }
                } else {
                    return 0; /* samples [1, 0, 0] */
                }
            } else {
                if (features[0] <= 58.049999237060547) { /* temperature_c */
                    if (features[4] <= 1.1431499719619751) { /* flame_analog_volts */
                        return 1; /* samples [0, 1, 0] */
                    } else {
                        return 2; /* samples [0, 0, 1] */
                    }
                } else {
                    return 2; /* samples [0, 0, 1] */
                }
            }
        }
    } else {
        return 2; /* samples [0, 0, 1] */
    }
}

/** Human-readable label for a class index, or "UNKNOWN" if out of range. */
static inline const char *fire_model_label(int class_index) {
    if (class_index < 0 || class_index >= FIRE_MODEL_N_CLASSES) {
        return "UNKNOWN";
    }
    return FIRE_MODEL_LABELS[class_index];
}

#ifdef __cplusplus
}
#endif

#endif /* FIRE_MODEL_H */
