from typing import Tuple, Optional

import torch.nn as nn
import torch
from torch_geometric.nn import GCNConv, SimpleConv, GATConv, GAT, GPSConv, LayerNorm, summary
from torch_geometric.loader import DataLoader
from torch_geometric.data import Batch, Dataset
from torch_geometric.data import Data
from torch_geometric.data.batch import Batch
from torch_geometric.utils import sort_edge_index
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

from src.utils import get_mesh_lat_long


class MLP(nn.Module):
    def __init__(self, mlp_config: MLPBlock, input_dim):
        super().__init__()
        hidden_dims = mlp_config.mlp_hidden_dims
        output_dim = (
            mlp_config.output_dim
        )  # TODO, this should not be hardcoded but come from "F"

        self.MLP = nn.ModuleList()
        in_features_for_last_layer = input_dim
        if hidden_dims:
            self.MLP.extend(
                [
                    nn.Linear(
                        in_features=input_dim,
                        out_features=hidden_dims[0],
                    ),
                    nn.PReLU(),
                ]
            )

            for h_index in range(1, len(hidden_dims)):
                self.MLP.extend(
                    [
                        nn.Linear(
                            in_features=hidden_dims[h_index - 1],
                            out_features=hidden_dims[h_index],
                        ),
                        nn.PReLU(),
                    ]
                )
            in_features_for_last_layer = hidden_dims[-1]

        self.MLP.append(
            nn.Linear(in_features=in_features_for_last_layer, out_features=output_dim)
        )

        if mlp_config.use_layer_norm:
            self.MLP.append(
                LayerNorm(in_channels=output_dim, mode=mlp_config.layer_norm_mode)
            )

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
            self.layers = SimpleConv(aggr="mean")

        elif graph_config.layer_type in [GraphLayerType.ConvGCN, GraphLayerType.GATConv]:
            self.activation = torch.nn.PReLU()
            self.output_dim = graph_config.output_dim
            self.layers = torch.nn.ModuleList()
            hidden_dims = graph_config.hidden_dims
            
            if graph_config.layer_type == GraphLayerType.ConvGCN:
                self.layers.append(GCNConv(input_dim, hidden_dims[0]))
                self.layers.append(self.activation)

                for i in range(1, len(hidden_dims)):
                    self.layers.append(GCNConv(hidden_dims[i - 1], hidden_dims[i]))
                    self.layers.append(self.activation)

                self.layers.append(GCNConv(hidden_dims[-1], graph_config.output_dim))

            elif graph_config.layer_type == GraphLayerType.GATConv:
                print('GAT')
                self.layers.append(GATConv(input_dim, hidden_dims[0], heads=1))
                self.layers.append(self.activation)

                for i in range(1, len(hidden_dims)):
                    self.layers.append(GATConv(hidden_dims[i - 1], hidden_dims[i]))
                    # , heads=1, concat=True
                    self.layers.append(self.activation)

                self.layers.append(GATConv(hidden_dims[-1], graph_config.output_dim, heads=1))
                # self.layers.append(GAT(in_channels=input_dim, hidden_channels=hidden_dims[0], num_layers=len(hidden_dims), out_channels=self.output_dim, act=self.activation, heads=1))

            if graph_config.use_layer_norm:
                self.layers.append(
                    LayerNorm(
                        in_channels=graph_config.output_dim,
                        mode=graph_config.layer_norm_mode,
                    )
                )


        else:
            raise NotImplementedError(
                f"Layer type {graph_config.layer_type} not supported."
            )

    def forward(self, X: torch.Tensor, edge_index: torch.Tensor):
        # batch_size, num_nodes, num_features = X.shape
        # data = [Data(x=sample, edge_index=edge_index) for sample in X]
        # # Issue of non-convergence likely stems from here
        # loader = DataLoader(data, batch_size=16, shuffle=True)
        # print('X shape: ', X.shape)
        # print('X shape: ', X.squeeze().shape)

        if self.layer_type == GraphLayerType.SimpleConv:
            return self.layers(x=X, edge_index=edge_index)

        elif self.layer_type == GraphLayerType.ConvGCN:
            for layer in self.layers:
                if type(layer) == GCNConv:
                    X = layer(X, edge_index)
                else:
                    X = layer(X)
            # x = None
            # for batch in loader:
            #     x = batch.x
            #     for layer in self.layers:
            #         if type(layer) == GCNConv:
            #             x = layer(x, edge_index)
            #         # If it is an activation function
            #         else:
            #             x = layer(x)
            # X = x.reshape(batch_size, num_nodes, num_features)

        elif self.layer_type == GraphLayerType.GATConv:
            for layer in self.layers:
                if type(layer) == GATConv:
                    X = layer(X, edge_index)
                else:
                    X = layer(X)
            # x = None
            # # attention_scores = []
            # for batch in loader:
            #     x = batch.x
            #     for layer in self.layers:
            #         if type(layer) == GATConv:
            #             x, (attn_edge_index, attn_weights) = layer(x, edge_index, return_attention_weights=True)
            #             # HOW TO INTERPRET ATTENTION SCORES?
            #             # ANSWER: The edge index returned is the edge index of the attention scores and give the score of (src, dst) pair
            #             # print('edge index shape: ', edge_index.shape)
            #             # print('Attention scores:', attn_weights.shape)
            #         # If it is an activation function
            #         else:
            #             x = layer(x)
            # X = x.reshape(batch_size, num_nodes, num_features)
        
        return X


class Model(nn.Module):
    """
    This is a generic model that can be used to define the encoder, processor, and decoder models.
    The model can have an MLP layer that can be used to process the input features before passing them through
    the graph layer. The graph layer will be used to process the input features and return the output features.
    """
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
        device,
    ):
        super().__init__()

        self.residual_output = pipeline_config.residual_output
        self.device = device
        self.obs_window = data_config.obs_window_used
        self.num_features = data_config.num_features_used
        self.total_feature_size = self.obs_window * self.num_features

        self._init_grid_properties(grid_lat=cordinates[0], grid_lon=cordinates[1])
        self._init_mesh_properties(graph_config)

        self._num_nodes = self._num_grid_nodes + self._num_mesh_nodes

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

        self.init_grid_features, self.init_mesh_features = self.init_grid_features.to(
            device
        ), self.init_mesh_features.to(device)

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

        encoder_input_dim = self.total_feature_size + self._init_feature_size
        self.encoder = Model(
            model_config=pipeline_config.encoder,
            input_dim=encoder_input_dim,
        )

        self.processor = Model(
            model_config=pipeline_config.processor, 
            input_dim=self.encoder.output_dim,
        )

        self.decoder = Model(
            model_config=pipeline_config.decoder,
            input_dim=self.processor.output_dim,
        )

        self.encoding_graph, self.decoding_graph, self.processing_graph = (
            self.encoding_graph.to(self.device),
            self.decoding_graph.to(device),
            self.processing_graph.to(device),
        )

        print('Encoder summary: ')
        print(summary(self.encoder, torch.randn(self._num_nodes, encoder_input_dim), self.encoding_graph))
        print()

        print('Processor summary: ')
        print(summary(self.processor, torch.randn(self._num_nodes, self.encoder.output_dim), self.processing_graph))
        print()

        print('Decoder summary: ')
        print(summary(self.decoder, torch.randn(self._num_nodes, self.processor.output_dim), self.decoding_graph))
        print()

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
        ).to(self.device)

        updated_mesh_node_features = torch.cat(
            (mesh_node_features, broadcasted_mesh_grid_features), dim=-1
        )

        # Concatenate them into one single tensor so that they can be passed through graph layers
        X = torch.cat((updated_grid_node_features, updated_mesh_node_features), dim=1)

        return X
    

    def get_edge_indices(self):
        """Returns the edge indices of the three graphs that are used in the model."""
        return self.encoding_graph, self.processing_graph, self.decoding_graph
    
    def get_num_grid_mesh_nodes(self):
        """Returns the number of grid and mesh nodes."""
        return self._num_grid_nodes, self._num_mesh_nodes

    def forward(self, data: Batch):
        """The forward method takes the features of the grid nodes and passes them through the three graphs defined above.
        Grid2Mesh performs the encoding and calculates the

        Parameters
        ----------
        X : torch.Tensor
          The input data of the shape [batch, num_grid_nodes, num_features].
        """
        # print('data.x shape: ', data.x.shape)
        # print('edge index encoder: ', sort_edge_index(data.edge_index_encoder).max(dim=1))
        # print("edge index processor: ", sort_edge_index(data.edge_index_processor))
        # print('edge index decoder: ', sort_edge_index(data.edge_index_decoder).max(dim=1))

        # print('batch size, num mesh nodes, num features: ', data.batch_size, data.num_mesh_nodes, data.num_features)
        # print('batch size, num grid nodes, num features: ', data.batch_size, data.num_grid_nodes, data.num_features)
        B = data.batch_size
        X = data.x.reshape(data.batch_size, data.num_nodes // data.batch_size, data.num_features)
        # print('X shape: ', X.shape)
        X = self._preprocess_input(grid_node_features=X)
        X = X.reshape(B * self._num_nodes, -1)
        # print('X shape: ', X.shape)

        
        # print('X shape: ', X.shape)
        encoded_features = self.encoder(X=data.x, edge_index=data.edge_index_encoder)
        # print('Encoded features shape: ', encoded_features.shape)

        encoded_features = encoded_features.reshape(B, self._num_nodes, -1)
        grid_node_features = encoded_features[:, :self._num_grid_nodes]
        mesh_node_features = encoded_features[:, self._num_grid_nodes:]
        # print('Grid node features shape: ', grid_node_features.shape)
        # print('Mesh node features shape: ', mesh_node_features.shape)
        grid_node_features = grid_node_features.reshape(B * self._num_grid_nodes, -1)
        mesh_node_features = mesh_node_features.reshape(B * self._num_mesh_nodes, -1)

        # Processing the mesh node features
        processed_mesh_node_features = self.processor(
            X=mesh_node_features, edge_index=data.edge_index_processor
        )
        # print('Processed mesh node features shape: ', processed_mesh_node_features.shape)

        # Concatenating the grid feature again with the processed mesh features
        processed_features = torch.cat(
            (grid_node_features, processed_mesh_node_features), dim=0
        )

        decoded_grid_node_features = self.decoder(
            X=processed_features,
            edge_index=data.edge_index_decoder,
        )
        # print('Decoded grid node features shape: ', decoded_grid_node_features.shape)
        decoded_grid_node_features = decoded_grid_node_features.reshape(B, self._num_nodes, -1)
        decoded_grid_node_features = decoded_grid_node_features[:, :self._num_grid_nodes]
        decoded_grid_node_features = decoded_grid_node_features.reshape(B * self._num_grid_nodes, -1)

        if self.residual_output:
            # TODO: Support residual outputs
            pass

        return decoded_grid_node_features
