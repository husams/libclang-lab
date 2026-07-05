// manifests/locals.cpp -- body-local declarations for the local-symbol indexer.
//
// Exercises every declaration kind the symbol pass must now pick up from INSIDE
// function and method bodies (local using/typedef/enum/records + local-record
// members, at arbitrary nesting), PLUS local variables that must STAY unindexed
// (they are reference-site sources only), PLUS an expression-context reference
// to a local alias that must resolve to a `uses` edge once the alias is a
// symbol. Namespace-scope aliases and globals stay indexed exactly as before.

using GlobalAlias = int;      // namespace-scope alias   (indexed as before)
int global_counter = 0;       // global variable         (indexed as before)
static int static_global = 1; // static global variable  (indexed as before)

int free_fn(int n) {
  using LocalAlias = int;          // TYPE_ALIAS_DECL      (local)
  typedef int LocalTypedef;        // TYPEDEF_DECL         (local)
  enum LocalEnum { LE_A, LE_B };   // ENUM_DECL + 2x ENUM_CONSTANT_DECL (local)
  struct LocalStruct {             // STRUCT_DECL          (local)
    int field;                     //   FIELD_DECL         (local-record member)
    int method() const { return field; } // CXX_METHOD     (local-record member)
  };
  union LocalUnion {               // UNION_DECL           (local)
    int i;
    float f;
  };

  LocalAlias a = n;                // local variable  -> NOT a symbol
  LocalTypedef t = a;              // local variable  -> NOT a symbol
  LocalStruct s{t};                // local variable  -> NOT a symbol
  LocalUnion u;                    // local variable  -> NOT a symbol
  u.i = 0;
  int sz = sizeof(LocalAlias);     // expr-context TYPE_REF -> uses(LocalAlias)
  return s.method() + LE_A + u.i + sz;
}

struct Host {
  int run(int x);
};

int Host::run(int x) {
  class LocalClass {               // CLASS_DECL in a METHOD body
  public:
    int v;                         //   FIELD_DECL
    explicit LocalClass(int q) : v(q) {} // CONSTRUCTOR    (local-record member)
    ~LocalClass() {}               // DESTRUCTOR           (local-record member)
    int get() const { return v; }  // CXX_METHOD
    typedef int Inner;             // nested TYPEDEF inside a local class
  };
  LocalClass lc(x);                // local variable  -> NOT a symbol
  auto lam = [](int y) {           // lambda body
    using LambdaAlias = int;       // TYPE_ALIAS_DECL nested in a lambda body
    LambdaAlias z = y;             // local variable -> NOT a symbol
    return z;
  };
  return lc.get() + lam(x);
}

template <typename T> T tmpl_fn(T v) {
  typedef T TmplLocal;             // TYPEDEF inside a function-template body
  TmplLocal w = v;                 // local variable -> NOT a symbol
  return w;
}

int use_tmpl() { return tmpl_fn<int>(3); }
