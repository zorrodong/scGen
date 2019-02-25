import logging
import tensorflow
from scipy import sparse
from .util import label_encoder, shuffle_data


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__file__)


class CVAE:
    """
        Conditional-VAE with Arithmetic vector Network class. This class contains the implementation of Conditional
        Variational Auto-encoder network with Vector Arithmetic.

        Parameters
        ----------
        train_data : `~anndata.AnnData`
            Annotated data matrix. Note that `train_data.X` has to be in shape [n_obs, n_vars].
        conditions : dict
            dictionary of conditions of training dataset
        dr_rate: float
            dropout rate
        learning_rate: float
            learning rate of optimization algorithm
        batch_size: integer
            size of each batch of training dataset to be fed to network while training.
        z_dimension: integer
            number of latent space dimensions.
        use_validation: bool
            if `True`: must put `validation_data` in `kwargs`
        shuffle: bool
            if `True`: shuffles the training dataset
        model_path: basestring
            path to save the model after training
        kwargs:
            :key `validation_data` : must be fed if `use_validation` is true. Its value and type is the same as `train_data`.

        See also
        --------
        CVAE from scgen.models._cvae : Conditional VAE implementation.

    """
    def __init__(self, train_data, conditions, dr_rate=.2, learning_rate=0.001, batch_size=32,
                 z_dimension=100, alpha=0.1, beta=1, use_validation=False, shuffle=False, model_path="../models/scgen", **kwargs):

        self.train_data = train_data
        self.train_labels, self.label_encoder = label_encoder(train_data)
        if shuffle:
            self.train_data, self.train_labels = shuffle_data(self.train_data, self.train_labels)
            self.orig_data = train_data
        self.use_validation = use_validation
        if self.use_validation:
            self.valid_data = kwargs["validation_data"]
            self.valid_labels, _ = label_encoder(self.valid_data)
        self.x_dim = self.train_data.shape[1]
        self.is_training = tensorflow.placeholder(tensorflow.bool, name='training_flag')
        self.z_dim = z_dimension
        self.lr = learning_rate
        self.alpha = alpha
        self.beta = beta
        self.conditions = conditions
        self.dr_rate = dr_rate
        self.model_to_use = model_path
        self.batch_size = batch_size
        self.global_step = tensorflow.Variable(0, name='global_step', trainable=False, dtype=tensorflow.int32)
        self.x = tensorflow.placeholder(tensorflow.float32, shape=[None, self.x_dim ], name="data")
        self.z = tensorflow.placeholder(tensorflow.float32, shape=[None, self.z_dim], name="latent")
        self.y = tensorflow.placeholder(tensorflow.float32, shape=[None, 1], name="labels")
        self.time_step = tensorflow.placeholder(tensorflow.int32)
        self.size = tensorflow.placeholder(tensorflow.int32)
        self.init_w = tensorflow.contrib.layers.xavier_initializer()
        self._create_network()
        self._loss_function()
        init = tensorflow.global_variables_initializer()
        self.sess = tensorflow.InteractiveSession()
        self.saver = tensorflow.train.Saver(max_to_keep=1)
        self.sess.run(init)

    def _encoder(self):
        """
            Constructs the encoder sub-network of C-VAE. This function implements the
            encoder part of Variational Auto-encoder. It will transform primary
            data in the `n_vars` dimension-space to the `z_dimension` latent space.

            Parameters
            ----------
            No parameters are needed.

            Returns
            -------
            mean: Tensor
                A dense layer consists of means of gaussian distributions of latent space dimensions.
            log_var: Tensor
                A dense layer consists of log transformed variances of gaussian distributions of latent space dimensions.
        """
        with tensorflow.variable_scope("encoder", reuse=tensorflow.AUTO_REUSE):
            xy = tensorflow.concat([self.x, self.y], axis=1)
            h = tensorflow.layers.dense(inputs=xy, units=700, kernel_initializer=self.init_w, use_bias=False)
            h = tensorflow.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tensorflow.nn.leaky_relu(h)
            h = tensorflow.layers.dropout(h, self.dr_rate, training=self.is_training)
            h = tensorflow.layers.dense(inputs=h, units=400, kernel_initializer=self.init_w, use_bias=False)
            h = tensorflow.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tensorflow.nn.leaky_relu(h)
            h = tensorflow.layers.dropout(h,self.dr_rate, training=self.is_training)
            mean = tensorflow.layers.dense(inputs=h, units=self.z_dim, kernel_initializer=self.init_w)
            log_var = tensorflow.layers.dense(inputs=h, units=self.z_dim, kernel_initializer=self.init_w)
            return mean, log_var

    def _mmd_decoder(self):
        """
            Constructs the decoder sub-network of C-VAE. This function implements the
            decoder part of Variational Auto-encoder. It will transform constructed
            latent space to the previous space of data with n_dimensions = n_vars.

            Parameters
            ----------
            No parameters are needed.

            Returns
            -------
            h: Tensor
                A Tensor for last dense layer with the shape of [n_vars, ] to reconstruct data.

        """
        with tensorflow.variable_scope("decoder", reuse=tensorflow.AUTO_REUSE):
            xy = tensorflow.concat([self.z_mean, self.y], axis=1)
            h_mmd = tensorflow.layers.dense(inputs=xy, units=400, kernel_initializer=self.init_w, use_bias=False)
            h = tensorflow.layers.batch_normalization(h_mmd, axis=1, training= self.is_training, )
            h = tensorflow.nn.leaky_relu(h)
            h = tensorflow.layers.dense(inputs=h, units=700, kernel_initializer=self.init_w, use_bias=False)
            tensorflow.layers.batch_normalization(h, axis=1, training=self.is_training)
            h = tensorflow.nn.leaky_relu(h)
            h = tensorflow.layers.dropout(h, self.dr_rate, training=self.is_training)
            h = tensorflow.layers.dense(inputs=h, units=self.x_dim, kernel_initializer=self.init_w, use_bias=True)
            h = tensorflow.nn.relu(h)
            return h

    def _sample_z(self):
        """
            Samples from standard Normal distribution with shape [size, z_dim] and
            applies re-parametrization trick. It is actually sampling from latent
            space distributions with N(mu, var) computed in `_encoder` function.

            Parameters
            ----------
            No parameters are needed.

            Returns
            -------
            The computed Tensor of samples with shape [size, z_dim].
        """
        eps = tensorflow.random_normal(shape=[self.size, self.z_dim])
        return self.mu + tensorflow.exp(self.log_var / 2) * eps

    def _create_network(self):
        """
            Constructs the whole C-VAE network. It is step-by-step constructing the C-VAE
            network. First, It will construct the encoder part and get mu, log_var of
            latent space. Second, It will sample from the latent space to feed the
            decoder part in next step. Finally, It will reconstruct the data by
            constructing decoder part of C-VAE.

            Parameters
            ----------
            No parameters are needed.

            Returns
            -------
            Nothing will be returned.
        """
        self.mu, self.log_var = self._encoder()
        self.z_mean = self._sample_z()
        self.x_hat = self._mmd_decoder()

    @staticmethod
    def compute_kernel(x, y):
        """
            Computes RBF kernel between x and y.

            Parameters
            ----------
            x: Tensor
                Tensor with shape [batch_size, z_dim]
            y: Tensor
                Tensor with shape [batch_size, z_dim]

            Returns
            -------
                returns the computed RBF kernel between x and y
        """
        x_size = tensorflow.shape(x)[0]
        y_size = tensorflow.shape(y)[0]
        dim = tensorflow.shape(x)[1]
        tiled_x = tensorflow.tile(tensorflow.reshape(x, tensorflow.stack([x_size, 1, dim])), tensorflow.stack([1, y_size, 1]))
        tiled_y = tensorflow.tile(tensorflow.reshape(y, tensorflow.stack([1, y_size, dim])), tensorflow.stack([x_size, 1, 1]))
        return tensorflow.exp(-tensorflow.reduce_mean(tensorflow.square(tiled_x - tiled_y), axis=2) / tensorflow.cast(dim, tensorflow.float32))

    @staticmethod
    def compute_mmd(x, y):  # [batch_size, z_dim] [batch_size, z_dim]
        """
            Computes Maximum Mean Discrepancy(MMD) between x and y.

            Parameters
            ----------
            x: Tensor
                Tensor with shape [batch_size, z_dim]
            y: Tensor
                Tensor with shape [batch_size, z_dim]

            Returns
            -------
                returns the computed MMD between x and y
        """
        x_kernel = CVAE.compute_kernel(x, x)
        y_kernel = CVAE.compute_kernel(y, y)
        xy_kernel = CVAE.compute_kernel(x, y)
        return tensorflow.reduce_mean(x_kernel) + tensorflow.reduce_mean(y_kernel) - 2 * tensorflow.reduce_mean(xy_kernel)

    def _loss_function(self):
        """
            Defines the loss function of C-VAE network after constructing the whole
            network. This will define the KL Divergence and Reconstruction loss for
            C-VAE and also defines the Optimization algorithm for network. The C-VAE Loss
            will be weighted sum of reconstruction loss and KL Divergence loss.

            Parameters
            ----------
            No parameters are needed.

            Returns
            -------
            Nothing will be returned.
        """
        self.kl_loss = 0.5 * tensorflow.reduce_sum(tensorflow.exp(self.log_var) + tensorflow.square(self.mu) - 1. - self.log_var, 1)
        self.recon_loss = 0.5 * tensorflow.reduce_sum(tensorflow.square((self.x - self.x_hat)), 1)
        self.vae_loss = tensorflow.reduce_mean(self.recon_loss + self.alpha*self.kl_loss)
        with tensorflow.control_dependencies(tensorflow.get_collection(tensorflow.GraphKeys.UPDATE_OPS)):
            self.solver = tensorflow.train.AdamOptimizer(learning_rate=self.lr).minimize(self.vae_loss)

    def _to_latent(self, data, labels):
        """
            Map `data` in to the latent space. This function will feed data
            in encoder part of C-VAE and compute the latent space coordinates
            for each sample in data.

            Parameters
            ----------
            data: `~anndata.AnnData`
                Annotated data matrix to be mapped to latent space. `data.X` has to be in shape [n_obs, n_vars].

            labels: numpy nd-array
                `numpy nd-array` of labels to be fed as CVAE's condition array.

            Returns
            -------
            latent: numpy nd-array
                returns array containing latent space encoding of 'data'
        """
        latent = self.sess.run(self.z_mean, feed_dict={self.x: data, self.y: labels,
                                                       self.size: data.shape[0], self.is_training: False})
        return latent

    def _reconstruct(self, data, labels, use_data=False):
        """
            Map back the latent space encoding via the decoder.

            Parameters
            ----------
            data: `~anndata.AnnData`
                Annotated data matrix whether in latent space or primary space.

            labels: numpy nd-array
                `numpy nd-array` of labels to be fed as CVAE's condition array.

            use_data: bool
                this flag determines whether the `data` is already in latent space or not.
                if `True`: The `data` is in latent space (`data.X` is in shape [n_obs, z_dim]).
                if `False`: The `data` is not in latent space (`data.X` is in shape [n_obs, n_vars]).

            Returns
            -------
            rec_data: 'numpy nd-array'
                returns 'numpy nd-array` containing reconstructed 'data' in shape [n_obs, n_vars].
        """
        if use_data:
            latent = data
        else:
            latent = self._to_latent(data, labels)
        rec_data = self.sess.run(self.x_hat, feed_dict={self.z_mean: latent, self.y: labels.reshape(-1, 1), self.is_training: False})
        return rec_data

    def predict(self, data, labels):
        """
            Predicts the cell type provided by the user in stimulated condition.

            Parameters
            ----------
            data: `~anndata.AnnData`
                Annotated data matrix whether in primary space.

            labels: numpy nd-array
                `numpy nd-array` of labels to be fed as CVAE's condition array.

            Returns
            -------
            stim_pred: numpy nd-array
                `numpy nd-array` of predicted cells in primary space.

            Example
            --------
            >>> import scanpy as sc
            >>> import scgen
            >>> train_data = sc.read("train_kang.h5ad")
            >>> validation_data = sc.read("./data/validation.h5ad")
            >>> network = scgen.CVAE(train_data=train_data, use_validation=True, validation_data=validation_data, model_path="./saved_models/", conditions={"ctrl": "control", "stim": "stimulated"})
            >>> network.train(n_epochs=20)
            >>> prediction = network.predict('CD4T', obs_key={"cell_type": ["CD8T", "NK"]})
        """
        if sparse.issparse(data.X):
            stim_pred = self._reconstruct(data.X.A, labels)
        else:
            stim_pred = self._reconstruct(data.X, labels)
        return stim_pred

    def restore_model(self):
        """
            restores model weights from `model_to_use`.

            Parameters
            ----------
            No parameters are needed.

            Returns
            -------
            Nothing will be returned.

            Example
            --------
            >>> import scanpy as sc
            >>> import scgen
            >>> train_data = sc.read("./data/train_kang.h5ad")
            >>> validation_data = sc.read("./data/valiation.h5ad")
            >>> network = scgen.CVAE(train_data=train_data, use_validation=True, validation_data=validation_data, model_path="./saved_models/", conditions={"ctrl": "control", "stim": "stimulated"})
            >>> network.restore_model()
        """
        self.saver.restore(self.sess, self.model_to_use)

    def train(self, n_epochs, early_stop_limit=20, threshold=0.0025, initial_run=True):
        """
            Trains the network `n_epochs` times with given `train_data`
            and validates the model using validation_data if it was given
            in the constructor function. This function is using `early stopping`
            technique to prevent overfitting.

            Parameters
            ----------
            n_epochs: int
                number of epochs to iterate and optimize network weights

            early_stop_limit: int
                number of consecutive epochs in which network loss is not going lower.
                After this limit, the network will stop training.

            threshold: float
                Threshold for difference between consecutive validation loss values
                if the difference is upper than this `threshold`, this epoch will not
                considered as an epoch in early stopping.
            full_training: bool
                if `True`: Network will be trained with all batches of data in each epoch.
                if `False`: Network will be trained with a random batch of data in each epoch.
            initial_run: bool
                if `True`: The network will initiate training and log some useful initial messages.
                if `False`: Network will resume the training using `restore_model` function in order
                    to restore last model which has been trained with some training dataset.


            Returns
            -------
            Nothing will be returned

            Example
            --------
            >>> import scanpy as sc
            >>> import scgen
            >>> train_data = sc.read(train_katrain_kang.h5ad           >>> validation_data = sc.read(valid_kang.h5ad)
            >>> network = scgen.CVAE(train_data=train_data, use_validation=True, validation_data=validation_data, model_path="./saved_models/", conditions={"ctrl": "control", "stim": "stimulated"})
            >>> network.train(n_epochs=20)
        """
        if initial_run:
            log.info("----Training----")
            assign_step_zero = tensorflow.assign(self.global_step, 0)
            _init_step = self.sess.run(assign_step_zero)
        if not initial_run:
            self.saver.restore(self.sess, self.model_to_use)
        loss_hist = []
        patience = early_stop_limit
        min_delta = threshold
        patience_cnt = 0
        for it in range(n_epochs):
            increment_global_step_op = tensorflow.assign(self.global_step,  self.global_step + 1)
            _step = self.sess.run(increment_global_step_op)
            current_step = self.sess.run(self.global_step)
            train_loss = 0
            for lower in range(0, self.train_data.shape[0], self.batch_size):
                upper = min(lower + self.batch_size, self.train_data.shape[0])
                if sparse.issparse(self.train_data.X):
                    x_mb = self.train_data[lower:upper, :].X.A
                else:
                    x_mb = self.train_data[lower:upper, :].X
                y_mb = self.train_labels[lower:upper]
                _, current_loss_train = self.sess.run([self.solver, self.vae_loss],
                                                      feed_dict={self.x: x_mb, self.y: y_mb, self.time_step:current_step,
                                                                 self.size: len(x_mb), self.is_training: True})
                train_loss += current_loss_train
            if self.use_validation:
                valid_loss = 0
                for lower in range(0, self.valid_data.shape[0], self.batch_size):
                    upper = min(lower + self.batch_size, self.valid_data.shape[0])
                    if sparse.issparse(self.valid_data.X):
                        x_mb = self.valid_data[lower:upper, :].X.A
                    else:
                        x_mb = self.valid_data[lower:upper, :].X
                    y_mb = self.valid_labels[lower:upper]
                    current_loss_valid = self.sess.run(self.vae_loss, feed_dict={self.x: x_mb, self.y: y_mb, self.time_step: current_step,
                                                                                 self.size: len(x_mb), self.is_training: False})
                    valid_loss += current_loss_valid
                loss_hist.append(valid_loss / self.valid_data.shape[0])
                if it > 0 and loss_hist[it - 1] - loss_hist[it] > min_delta:
                    patience_cnt = 0
                else:
                    patience_cnt += 1
                if patience_cnt > patience:
                    save_path = self.saver.save(self.sess, self.model_to_use)
                    break
        else:
            save_path = self.saver.save(self.sess, self.model_to_use)
        log.info(f"Model saved in file: {save_path}. Training finished")
