#include <stdlib.h>

int GlobalCounter = 0;                    /* PascalCase global */

int BadlyNamedFunction(int A, int B) {    /* PascalCase fn, 1-letter params */
    int Result = 0;
    if (A > 0) {
        if (B > 0) {
            if (A > B) {
                if (A - B > 10) {
                    Result = A - B;       /* nesting depth 4 */
                } else {
                    Result = B - A;
                }
            }
        }
    }
    return Result;
}

int ok_function(int value) {
    return value + GlobalCounter;
}
