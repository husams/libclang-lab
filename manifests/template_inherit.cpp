// Regression fixture: class templates that inherit from a *concrete*
// (non-dependent) base class.
//
// Bug: the CXX_BASE_SPECIFIER handler in the edge extractor only accepted a
// CLASS_DECL / STRUCT_DECL walk-parent, so a base specifier nested under a
// CLASS_TEMPLATE (or its partial specialization) was silently dropped and no
// `inherits` (edge kind 2) edge was recorded.  Any inheritance query starting
// from such a template returned an empty parent list even though the source
// declares an unambiguous, non-dependent base.
//
// This mirrors the real-world report (`BzRuleTemplate : public BzRuleAbstract`).

namespace ti {

// Concrete, non-dependent base.
struct Rule {
    virtual int rank() const { return 0; }
    virtual ~Rule() {}
};

// (1) Class template with a concrete public base -> must emit inherits(2).
template <class Adapter, typename NameType>
class RuleTemplate : public Rule {
public:
    int rank() const override { return 1; }
    Adapter *adapter = nullptr;
    NameType name{};
};

// (2) Partial specialization with a concrete base -> must emit inherits(2).
template <typename NameType>
class RuleTemplate<int, NameType> : public Rule {
public:
    int rank() const override { return 2; }
    NameType name{};
};

// (3) Plain (non-template) derived class -> control; already worked.
struct PlainRule : public Rule {
    int rank() const override { return 3; }
};

// Force instantiation of both the primary template and the partial spec.
RuleTemplate<double, int> g_primary;
RuleTemplate<int, int> g_partial;
PlainRule g_plain;

}  // namespace ti
