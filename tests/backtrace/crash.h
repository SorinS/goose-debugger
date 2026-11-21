/* A C function that writes through a null pointer, for a crash inside code
   TinyCC compiled. */
void crash_store(long long v) { *(volatile long long *)0 = v; }
