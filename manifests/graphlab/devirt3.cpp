// devirt3.cpp -- Phase 3a value-ness cases + Phase 3b dispatch_param definition.
#include "devirt3.hpp"

namespace graphlab {

chain::B  g_b;              // value global (definition)
chain::B& g_ref = g_b;     // ref global (definition) -- NEGATIVE

// ---- 3a: POSITIVE cases (recv_type_is_value=1) ----
int HolderV::via()   { return b.rank();         }  // value member
int use_global()     { return g_b.rank();        }  // value global
int use_ret()        { return make_b().rank();   }  // by-value return

// ---- 3a: NEGATIVE cases (recv_type_is_value=0) ----
int HolderR::via()    { return br.rank();          }  // ref member
int HolderP::via()    { return bp->rank();          }  // ptr member
int HolderS::via()    { return sp->rank();          }  // smart-ptr member
int use_ref_global()  { return g_ref.rank();        }  // ref global  -- NEGATIVE
int use_ret_ref()     { return make_ref().rank();   }  // ref return  -- NEGATIVE
int use_ret_ptr()     { return make_bp()->rank();   }  // ptr return

// Definitions for make_b / make_ref / make_bp.
chain::B  make_b()   { return chain::B{}; }
chain::B& make_ref() { return g_b; }
chain::B* make_bp()  { return &g_b; }

// ---- 3b: param receiver (a is param 0, no in-TU caller) ----
int dispatch_param(chain::A& a) { return a.rank(); }

} // namespace graphlab
