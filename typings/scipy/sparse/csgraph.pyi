from scipy.sparse import csr_matrix

class MaximumFlowResult:
    flow_value: int
    flow: csr_matrix

def maximum_flow(csgraph: csr_matrix, source: int, sink: int) -> MaximumFlowResult: ...
