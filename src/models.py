from typing import Tuple

import torch.nn as nn
import torch
from torch_geometric.nn import GCNConv, SimpleConv
import numpy as np

from src.config import (
    ModelConfig,
    MLPBlock,
    GraphBlock,
    GraphLayerType,
    GraphBuildingConfig,
    DataConfig,
    PipelineConfig,
)
from src.create_graphs import (
    create_decoding_graph,
    create_processing_graph,
    create_encoding_graph,
)


from src.mesh.create_mesh import (
    get_hierarchy_of_triangular_meshes_for_sphere,
)

from src.utils import (
    get_bipartite_graph_spatial_features,
    get_mesh_lat_long,
    GraphStructure,
)


class MLP(nn.Module):
    def __init__(self, mlp_config: MLPBlock, input_dim):
        super().__init__()
        hidden_dims = mlp_config.mlp_hidden_dims
        output_dim = mlp_config.output_dim

        self.MLP = nn.ModuleList(
            [
                nn.Linear(
                    in_features=input_dim,
                    out_features=hidden_dims[0],
                ),
                nn.ReLU(),
            ]
        )

        for h_index in range(1, len(hidden_dims)):
            self.MLP.extend(
                [
                    nn.Linear(
                        in_features=hidden_dims[h_index - 1],
                        out_features=hidden_dims[h_index],
                    ),
                    nn.ReLU(),
                ]
            )

        self.MLP.append(nn.Linear(in_features=hidden_dims[-1], out_features=output_dim))

    def forward(self, X: torch.Tensor):
        for layer in self.MLP:
            X = layer(X)

        return X


class GraphLayer(nn.Module):
    def __init__(self, graph_config: GraphBlock, input_dim):
        super().__init__()

        self.layer_type: GraphLayerType = graph_config.layer_type
        self.output_dim = None

        if graph_config.layer_type == GraphLayerType.SimpleConv:
            self.output_dim = input_dim
            self.layers = SimpleConv()

        elif graph_config.layer_type == GraphLayerType.ConvGCN:
            self.output_dim = graph_config.output_dim
            self.layers = torch.nn.ModuleList()
            hidden_dims = graph_config.hidden_dims

            self.layers.append(GCNConv(input_dim, hidden_dims[0]))
            for i in range(1, len(hidden_dims)):
                self.layers.append(GCNConv(hidden_dims[i - 1], hidden_dims[i]))

            self.layers.append(GCNConv(hidden_dims[-1], graph_config.output_dim))
            self.activation = torch.nn.ReLU()

        else:
            raise NotImplementedError(
                f"Layer type {graph_config.layer_type} not supported."
            )

    def forward(self, X: torch.Tensor, edge_index: torch.Tensor):
        if self.layer_type == GraphLayerType.SimpleConv:
            return self.layers(x=X, edge_index=edge_index)

        elif self.layer_type == GraphLayerType.ConvGCN:
            for layer in self.layers[:-1]:
                X = self.activation(layer(X, edge_index))

            X = self.layers[-1](X, edge_index)

        return X


class Model(nn.Module):
    def __init__(self, model_config: ModelConfig, input_dim: int):
        super().__init__()
        self.mlp = None
        self.output_dim = None
        graph_input_dim = input_dim
        if model_config.mlp:
            self.mlp = MLP(mlp_config=model_config.mlp, input_dim=input_dim)
            graph_input_dim = model_config.mlp.output_dim

        self.graph_layer = GraphLayer(
            graph_config=model_config.gcn, input_dim=graph_input_dim
        )
        self.output_dim = self.graph_layer.output_dim

    def forward(self, X: torch.Tensor, edge_index: torch.Tensor):

        if self.mlp:
            X = self.mlp(X=X)

        X = self.graph_layer(X=X, edge_index=edge_index)

        return X


class WeatherPrediction(nn.Module):
    """This is our main weather prediction model. Similar to GraphCast, this model will
      operate on three graphs -

    * Encoding graph: This graph contains all the nodes. This graph is strictly
    bipartite with edges going from grid nodes to the mesh nodes.
    The output of this stage will be a latent representation for
    the mesh nodes.

    * Processing Graph: This graph contains only the mesh nodes.
    It will update the latent state of the mesh nodes.

    * Decoding graph: This graph contains all nodes. This graph is strictly
      bipartite with edges going from mesh nodes to grid nodes such that each grid
      nodes is connected to 3 nodes of the mesh triangular face that contains
      the grid points. It will process the updated latent state of the mesh nodes, and the latent state
      of the grid nodes, to produce the final output for the grid nodes.
    """

    def __init__(
        self,
        cordinates: Tuple[np.array, np.array],
        graph_config: GraphBuildingConfig,
        pipeline_config: PipelineConfig,
        data_config: DataConfig,
    ):
        super().__init__()

        self.timesteps = data_config.num_timesteps
        self.num_features = data_config.num_features
        self.total_feature_size = self.timesteps * self.num_features

        self._init_grid_properties(grid_lat=cordinates[0], grid_lon=cordinates[1])
        self._init_mesh_properties(graph_config)

        self.encoding_graph, self.init_grid_features, self.init_mesh_features = (
            create_encoding_graph(
                grid_node_lats=self._grid_lat,
                grid_node_longs=self._grid_lon,
                mesh_node_lats=self._mesh_nodes_lat,
                mesh_node_longs=self._mesh_nodes_lon,
                mesh=self._finest_mesh,
                graph_building_config=graph_config,
                num_grid_nodes=self._num_grid_nodes,
            )
        )

        # The shape of the initial static features that are added to each node
        self._init_feature_size = self.init_grid_features.shape[1]

        self.processing_graph = create_processing_graph(
            meshes=self._meshes, mesh_levels=graph_config.mesh_levels
        )

        self.decoding_graph = create_decoding_graph(
            cordinates=cordinates,
            mesh=self._finest_mesh,
            graph_building_config=graph_config,
            num_grid_nodes=self._num_grid_nodes,
        )

        self.encoder = Model(
            model_config=pipeline_config.encoder,
            input_dim=self.total_feature_size + self._init_feature_size,
        )

        self.processor = Model(
            model_config=pipeline_config.processor, input_dim=self.encoder.output_dim
        )

        self.decoder = Model(
            model_config=pipeline_config.decoder, input_dim=self.processor.output_dim
        )

    def _init_grid_properties(self, grid_lat: np.ndarray, grid_lon: np.ndarray):
        self._grid_lat = grid_lat.astype(np.float32)
        self._grid_lon = grid_lon.astype(np.float32)
        self._num_grid_nodes = grid_lat.shape[0] * grid_lon.shape[0]

    def _init_mesh_properties(self, graph_config: GraphBuildingConfig):
        self._meshes = get_hierarchy_of_triangular_meshes_for_sphere(
            splits=graph_config.mesh_size
        )
        self._finest_mesh = self._meshes[-1]
        self._num_mesh_nodes = len(self._finest_mesh.vertices)

        self._mesh_nodes_lat, self._mesh_nodes_lon = get_mesh_lat_long(
            finest_mesh=self._finest_mesh
        )

        self._mesh_nodes_lat, self._mesh_nodes_lon = self._mesh_nodes_lat.astype(
            np.float32
        ), self._mesh_nodes_lon.astype(np.float32)

    def _preprocess_input(self, grid_node_features: torch.Tensor):
        batch_size, _, _ = grid_node_features.shape

        # Repeat initial features across all batches
        broadcasted_init_grid_features = self.init_grid_features.broadcast_to(
            (batch_size, self._num_grid_nodes, self._init_feature_size)
        )
        broadcasted_mesh_grid_features = self.init_mesh_features.broadcast_to(
            (batch_size, self._num_mesh_nodes, self._init_feature_size)
        )

        # Concatenate the initial grid node features with the incoming input
        updated_grid_node_features = torch.cat(
            (grid_node_features, broadcasted_init_grid_features), dim=-1
        )

        # Initialise the mesh node features to 0s and append the initial mesh features
        mesh_node_features = torch.zeros(
            (batch_size, self._num_mesh_nodes, self.total_feature_size)
        )

        updated_mesh_node_features = torch.cat(
            (mesh_node_features, broadcasted_mesh_grid_features), dim=-1
        )

        # Concatenate them into one single tensor so that they can be passed through graph layers
        X = torch.cat((updated_grid_node_features, updated_mesh_node_features), dim=1)

        return X

    def forward(self, X: torch.Tensor):
        """The forward method takes the features of the grid nodes and passes them through the three graphs defined above.
        Grid2Mesh performs the encoding and calculates the

        Parameters
        ----------
        X : torch.Tensor
          The input data of the shape [batch, num_grid_nodes, num_features].
        """

        X = self._preprocess_input(grid_node_features=X)

        encoded_features = self.encoder(X=X, edge_index=self.encoding_graph)

        grid_node_features = encoded_features[:, : self._num_grid_nodes, :]
        mesh_node_features = encoded_features[:, self._num_grid_nodes :, :]

        # Processing the mesh node features
        processed_mesh_node_features = self.processor(
            X=mesh_node_features, edge_index=self.processing_graph
        )

        # Concatenating the grid feature again with the processed mesh features
        processed_features = torch.cat(
            (grid_node_features, processed_mesh_node_features), dim=1
        )

        decoded_grid_node_features = self.decoder(
            X=processed_features,
            edge_index=self.decoding_graph,
        )

        decoded_grid_node_features = decoded_grid_node_features[
            :, : self._num_grid_nodes, :
        ]

        return decoded_grid_node_features
