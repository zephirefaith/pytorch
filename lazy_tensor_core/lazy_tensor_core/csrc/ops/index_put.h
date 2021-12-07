#pragma once

#include <torch/csrc/lazy/ts_backend/ts_node.h>

namespace torch_lazy_tensors {
namespace ir {
namespace ops {

class IndexPut : public torch::lazy::TsNode {
 public:
  IndexPut(const torch::lazy::Value& base, const torch::lazy::Value& indices,
           int64_t start_dim, const torch::lazy::Value& values,
           bool accumulate);

  std::string ToString() const override;

  int64_t start_dim() const { return start_dim_; }

  bool accumulate() const { return accumulate_; }

 private:
  // The dimension number at which indexing starts.
  int64_t start_dim_;
  // Whether to accumulate instead of set.
  bool accumulate_;
};

}  // namespace ops
}  // namespace ir
}  // namespace torch_lazy_tensors