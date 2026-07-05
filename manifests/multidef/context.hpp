#pragma once
// v27 multi-definition fixture: a library declares a method + static member
// variable and leaves them UNDEFINED; each "backend" (server1/server2) provides
// its own definition. cidx keys `symbol` by USR, so both bodies collapse to one
// node -- the definition / def_edge / possible_call tables keep every backend
// body, its own calls, and the "possible call" fan-out.
struct Context {
    void reg();               // declared here, defined per-backend
    void run() { reg(); }     // inline caller -> reg() (the lib's do())
    static int count;         // static member var, redefined per-backend
};
