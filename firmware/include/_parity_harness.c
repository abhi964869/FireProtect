#include <stdio.h>
#include <stdlib.h>
#include "fire_model.h"

int main(void) {
    float f[FIRE_MODEL_N_FEATURES];
    while (1) {
        for (int i = 0; i < FIRE_MODEL_N_FEATURES; i++) {
            if (scanf("%f", &f[i]) != 1) return 0;
        }
        printf("%d\n", fire_model_predict(f));
    }
}
