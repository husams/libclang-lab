// devirt3.hpp -- Phase 3 value-ness fixtures (3a: value member/global/return;
//                3b: cross-TU param union).
#pragma once
#include <memory>
#include "chain.hpp"            // chain::A, B, C, D + rank()

namespace graphlab {

// ---- 3a: value-ness cases ----
// HolderV: value member -- exact {B::rank} (POSITIVE)
struct HolderV { chain::B b;  int via(); };
// HolderR: ref member -- TOP (NEGATIVE)
struct HolderR { chain::B& br; HolderR(chain::B& x) : br(x) {} int via(); };
// HolderP: ptr member -- TOP (NEGATIVE)
struct HolderP { chain::B* bp = nullptr;  int via(); };
// HolderS: smart-ptr member -- TOP (NEGATIVE: shared_ptr USR != B USR)
struct HolderS { std::shared_ptr<chain::B> sp; int via(); };

extern chain::B  g_b;            // value global  -- POSITIVE (defined in devirt3.cpp)
extern chain::B& g_ref;          // ref global    -- NEGATIVE (defined in devirt3.cpp)
chain::B  make_b();              // by-value return -- POSITIVE
chain::B& make_ref();            // by-ref return  -- NEGATIVE
chain::B* make_bp();             // ptr return     -- NEGATIVE

int use_global();
int use_ref_global();            // ref global     -- NEGATIVE
int use_ret();
int use_ret_ref();               // ref return     -- NEGATIVE
int use_ret_ptr();

// ---- 3b: cross-TU param fixture ----
// dispatch_param is defined in devirt3.cpp; its sole caller is in devirt3_caller.cpp.
int dispatch_param(chain::A& a);

} // namespace graphlab
