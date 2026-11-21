/* A C function that writes through a null pointer, for a crash inside code
   TinyCC compiled. */
void crash_store(gs_i64 v) { *(volatile long long *)0 = v; }
