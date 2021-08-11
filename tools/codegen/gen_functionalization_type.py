from tools.codegen.api import cpp
from tools.codegen.api.types import DispatcherSignature, CppSignatureGroup
from tools.codegen.code_template import CodeTemplate
from tools.codegen.context import method_with_native_function
from tools.codegen.model import (
    SelfArgument, Argument, NativeFunction, NativeFunctionsGroup, BaseType, BaseTy,
    ListType, FunctionSchema
)
from typing import List, Optional, Union
from dataclasses import dataclass
from typing_extensions import Literal
from tools.codegen.utils import mapMaybe, Target, assert_never

from tools.autograd.gen_inplace_or_view_type import (
    gen_formals, get_view_info, modifies_arguments
)
from tools.autograd.gen_trace_type import (
    type_wrapper_name
)



WARN_TEMPLATE = CodeTemplate("""\
      if (c10::impl::tls_local_dispatch_key_set().included_.has(c10::DispatchKey::Functionalize)) {
          TORCH_WARN("Note: the functionalization pass encountered an operator (${op_name}) that it could not functionalize, \
because it couldn't find an out-of-place equivalent of the operator to call (or because it's a view operator \
that hasn't been implemented yet). Instead, it's calling the inplace/view operator directly. \
If this causes problems in your program, consider upstreaming the out-of-place op to PyTorch.");
      }
      at::AutoDispatchBelowFunctionalize guard;
      ${maybe_sync_tensor_args}
      // Redispatch as normally otherwise, since XLA has its own lowerings for special inplace ops.
      return at::redispatch::${cpp_api_name}(${unpacked_args});
""")

INPLACE_OR_OUT_TEMPLATE = CodeTemplate("""\
      ${maybe_unwrap_inputs}
      {
          at::AutoDispatchBelowFunctionalize guard;
          auto tmp_output = at::redispatch::${cpp_api_name}(${unpacked_args});
          ${mutable_input_post_processing}
      }
      return ${returns};
""")

VIEW_TEMPLATE = CodeTemplate("""\
      TORCH_INTERNAL_ASSERT_DEBUG_ONLY(self.key_set().has(c10::DispatchKey::Functionalize));
      ${return_type} out;
      // TODO: generalize this so we don't have to store it?
      auto original_size = self.sizes().vec();
      {
        at::AutoDispatchBelowFunctionalize guard;
        auto tmp_out = at::redispatch::${cpp_api_name}(${redispatch_original_args_unwrap_self});
        // See Note [Wrapping return values in the functionalization pass]
        bool is_modal_pass = c10::impl::tls_local_dispatch_key_set().included_.has(c10::DispatchKey::Functionalize);
        if (is_modal_pass) {
          auto self_impl = at::unsafeGetFunctionalImpl(self);
          ${clone_functionalize_output}
        } else {
          // NOTE: I'm primarily doing this to avoid adding 50 new native_functions.yaml operators
          // e.g. What we really want is a new view_copy() op (and similar for every other existing view)
          // Eager mode would implement this under the hood
          // XLA would just change their implementation of view to DO a copy.
          // We'd end up with an unnecessary clone() in the XLA case, but clones are cheap since we're just copying IR.
          ${clone_output}
        }
      }
      at::ViewMeta view_meta = ViewMeta(${view_meta_enum}, original_size, self.sizes().vec());
      ${set_view_meta_output}
      return out;
""")

def post_process_mutable_input(a: Argument) -> Optional[str]:
    if a.annotation and a.annotation.is_write and a.type.is_tensor_like():
        replace_str = f'{a.name}.replace_(tmp_output);'
        debug_str = f'TORCH_INTERNAL_ASSERT_DEBUG_ONLY({a.name}.key_set().has(c10::DispatchKey::Functionalize));'
        add_update_str = f'at::functionalization::impl::maybe_add_update({a.name});'
        return f'{replace_str}\n{debug_str}\n{add_update_str}'
    return None

def maybeUnwrapVarName(a: Argument) -> str:
    if not a.type.is_tensor_like():
        return a.name
    return f'{a.name}_'

def maybeUnwrapTensorInput(a: Argument) -> Optional[str]:
    t = a.type
    if not t.is_tensor_like():
        return None
    return f'auto {maybeUnwrapVarName(a)} = at::functionalization::impl::maybeUnwrapFunctional({a.name});'

def return_names_str(f: NativeFunction) -> str:
    if len(f.func.arguments.out) > 0:
        return ', '.join(a.name for a in f.func.arguments.out)
    if f.func.arguments.self_arg is not None:
        return f.func.arguments.self_arg.argument.name
    raise AssertionError("Unable to handle functionalization for op={str(f.func.name)}")

def gen_clone_output_str(func: FunctionSchema, functionalize: bool) -> str:
    if len(func.returns) == 1 and func.returns[0].type == BaseType(BaseTy.Tensor):
        if functionalize:
            return 'out = at::functionalization::impl::makeFunctional(tmp_out.clone());'
        else:
            return 'out = tmp_out.clone();'
    elif len(func.returns) == 1 \
            and isinstance(func.returns[0].type, ListType) \
            and func.returns[0].type.elem == BaseType(BaseTy.Tensor):
        if functionalize:
            return """\
for (const auto& t : tmp_out) {{
    out.push_back(at::functionalization::impl::makeFunctional(t.clone()));
}}"""
        else:
            return """\
for (const auto& t : tmp_out) {{
    out.push_back(t.clone());
}}"""
    else:
        raise AssertionError(f"unsupported return type for op={str(func.name)}. type={str(func.returns)}")

def set_view_meta_output(func: FunctionSchema) -> str:
    if len(func.returns) == 1 and func.returns[0].type == BaseType(BaseTy.Tensor):
        return 'at::functionalization::impl::set_view_meta(out, self, view_meta);'
    elif len(func.returns) == 1 \
            and isinstance(func.returns[0].type, ListType) \
            and func.returns[0].type.elem == BaseType(BaseTy.Tensor):
        return """\
for (auto& t : out) {{
    at::functionalization::impl::set_view_meta(t, self, view_meta);'
}}"""
    else:
        raise AssertionError(f"unsupported return type for op={str(func.name)}. type={str(func.returns)}")

def emit_functionalization_body(f: NativeFunction, g: Optional[NativeFunctionsGroup]) -> str:
    dispatcher_sig = DispatcherSignature.from_schema(f.func)
    dispatcher_exprs = dispatcher_sig.exprs()

    dispatch_key_set = 'ks & c10::after_func_keyset'
    redispatch_original_args = ', '.join([dispatch_key_set] + [a.name for a in dispatcher_sig.arguments()])

    # This is only used in view/inplace kernels that aren't implemented
    maybe_sync_tensor_args = '\n'.join(mapMaybe(
        lambda arg: f'at::functionalization::impl::maybe_sync({arg.name});' if arg.type.is_tensor_like() else None,
        f.func.arguments.flat_all))

    # Note that this calls the slow, dispatching variants of manual_cpp_binding ops.
    # We could probably work harder to ensure that the fast variants are called instead, but the perf benefit would be minimal.
    sig_group = CppSignatureGroup.from_native_function(f, method=False, fallback_binding=f.manual_cpp_binding)
    if sig_group.faithful_signature is not None:
        api_name = sig_group.faithful_signature.name()
    else:
        api_name = sig_group.signature.name()

    # view op case
    if get_view_info(f) is not None:
        return_type = dispatcher_sig.returns_type().remove_const_ref().cpp_type()
        # The codegen enforces the naming schema for different view metas.
        view_meta_enum = f'at::ViewMeta::Type::{str(f.func.name).replace(".", "_")}'

        clone_output = gen_clone_output_str(f.func, functionalize=False)
        clone_functionalize_output = gen_clone_output_str(f.func, functionalize=True)

        set_view_meta_output_str = set_view_meta_output(f.func)

        redispatch_original_args_unwrap_self = ', '.join([dispatch_key_set] + [
            a.name if not isinstance(a.argument, SelfArgument) else 'self_impl->value()' for a in dispatcher_sig.arguments()])

        if str(f.func.name) not in [
                # TODO: add view operators to this list as we add functionalization support for them.
                # More complex views might require changes to the codegen output (e.g. split())
                'view',
        ]:
            return WARN_TEMPLATE.substitute(
                op_name=str(f.func.name),
                cpp_api_name=api_name,
                maybe_sync_tensor_args=maybe_sync_tensor_args,
                unpacked_args=redispatch_original_args
            )

        return VIEW_TEMPLATE.substitute(
            return_type=return_type,
            cpp_api_name=api_name,
            view_meta_enum=view_meta_enum,
            redispatch_original_args=redispatch_original_args,
            redispatch_original_args_unwrap_self=redispatch_original_args_unwrap_self,
            clone_output=clone_output,
            clone_functionalize_output=clone_functionalize_output,
            set_view_meta_output=set_view_meta_output_str,
        )

    # mutation case
    assert(modifies_arguments(f))
    if g is None:
        # We can't functionalize this inplace op, since we don't know what the corresponding functional op is.
        cpp_sig = CppSignatureGroup.from_native_function(f, method=False, fallback_binding=f.manual_cpp_binding)
        return WARN_TEMPLATE.substitute(
            op_name=str(f.func.name),
            cpp_api_name=cpp_sig.most_faithful_signature().name(),
            maybe_sync_tensor_args=maybe_sync_tensor_args,
            unpacked_args=redispatch_original_args
        )
    # call the out-of-place variant of the op
    functional_cpp_sig = CppSignatureGroup.from_native_function(g.functional, method=False, fallback_binding=f.manual_cpp_binding)
    # The functionalization pass explicitly doesn't pass out= parameters to the redispatch
    redispatch_modified_args = ', '.join([dispatch_key_set] + [maybeUnwrapVarName(a) for a in f.func.arguments.flat_non_out])

    maybe_unwrap_inputs = '\n'.join([
        s for s in [maybeUnwrapTensorInput(a) for a in f.func.arguments.flat_non_out] if s is not None])

    mutable_input_post_processing = '\n'.join([
        s for s in [post_process_mutable_input(a) for a in f.func.arguments.flat_non_out] if s is not None])
    returns_str = return_names_str(f)
    if len(f.func.returns) > 1:
        returns_str = f'{dispatcher_sig.returns_type().cpp_type()}({returns_str});'

    return INPLACE_OR_OUT_TEMPLATE.substitute(
        maybe_unwrap_inputs=maybe_unwrap_inputs,
        cpp_api_name=functional_cpp_sig.most_faithful_signature().name(),
        unpacked_args=redispatch_modified_args,
        mutable_input_post_processing=mutable_input_post_processing,
        returns=returns_str
    )

METHOD_DEFINITION = CodeTemplate("""\
${return_type} ${type_wrapper_name}(${formals}) {
  ${type_definition_body}
}
""")

WRAPPER_REGISTRATION = CodeTemplate("""\
m.impl("${unqual_operator_name_with_overload}",
       TORCH_FN(${class_type}::${type_wrapper_name})
);
""")

# Generates RegisterFunctionalization.cpp
# These provide the kernels that run the functionalization pass, which can be opted into
# per backend (e.g. XLA or Vulkan), or as a composable transform (functionalize() in functorch).
@dataclass(frozen=True)
class Functionalize:
    target: Union[
        Literal[Target.REGISTRATION],
        Literal[Target.DEFINITION]
    ]

    @method_with_native_function
    def __call__(self, g: Union[NativeFunction, NativeFunctionsGroup]) -> List[str]:
        fs = [g] if isinstance(g, NativeFunction) else g.functions()
        group: Optional[NativeFunctionsGroup] = None if isinstance(g, NativeFunction) else g
        outputs = []
        for f in fs:
            if get_view_info(f) is None and not modifies_arguments(f):
                continue
            if len(f.func.returns) == 0:
                # TODO: it looks like all the _foreach_ ops fall into this category
                # Do they.. need to be functionalized?
                # print("OPERATOR: " + str(f.func.name))
                continue
            if get_view_info(f) is not None and modifies_arguments(f):  # view op
                # TODO: ops that are both views and mutations will require special handling. Pushing off for now.
                # e.g. transpose_, as_strided_
                continue
            if self.target is Target.REGISTRATION:
                output = WRAPPER_REGISTRATION.substitute(
                    unqual_operator_name_with_overload=f.func.name,
                    type_wrapper_name=type_wrapper_name(f),
                    class_type='functionalization',
                )
            elif self.target is Target.DEFINITION:
                output = METHOD_DEFINITION.substitute(
                    return_type=cpp.returns_type(f.func.returns).cpp_type(),
                    type_wrapper_name=type_wrapper_name(f),
                    formals=gen_formals(f),
                    type_definition_body=emit_functionalization_body(f, group),
                )
            elif self.target is Target.DECLARATION:
                raise NotImplementedError("The functionalization pass doesn't currently expose declarations")
            else:
                assert_never(self.target)
            outputs.append(output)
        return outputs
