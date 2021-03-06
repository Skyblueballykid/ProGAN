import datetime as dt
import os
import sys

# Operations used in building the network. Many are not used in the current model
from ops import *
# FeedDict object used to continuously provide new training data
from feed_dict import FeedDict


# TODO: add argparser and flags
# TODO: refactor training function
# TODO: train next version of model using reset_optimizer=True


class ProGAN:
    def __init__(self,
            logdir,                    # directory of stored models
            imgdir,                   # directory of images for FeedDict
            learning_rate=0.001,       # Adam optimizer learning rate
            beta1=0,                   # Adam optimizer beta1
            beta2=0.99,                # Adam optimizer beta2
            w_lambda=10.0,             # WGAN-GP/LP lambda
            w_gamma=1.0,               # WGAN-GP/LP gamma
            epsilon=0.001,             # WGAN-GP/LP lambda
            z_length=512,              # latent variable size
            n_imgs=800000,             # number of images to show in each growth step
            batch_repeats=1,           # number of times to repeat minibatch
            n_examples=24,             # number of example images to generate
            lipschitz_penalty=True,   # if True, use WGAN-LP instead of WGAN-GP
            big_image=True,            # Generate a single large preview image, only works if n_examples = 24
            scaling_factor=None,       # factor to scale down number of trainable parameters
            reset_optimizer=False,     # reset optimizer variables with each new layer
            use_uint8=False,
            batch_sizes=None,
            channels=None
    ):

        # Scale down the number of factors if scaling_factor is provided
        self.channels = channels if channels else [512, 512, 512, 512, 256, 128, 64, 32, 16, 16]
        if scaling_factor:
            assert scaling_factor > 1
            self.channels = [max(4, c // scaling_factor) for c in self.channels]

        self.batch_sizes = batch_sizes if batch_sizes else [16, 16, 16, 16, 16, 16, 8, 4, 3]
        self.z_length = z_length
        self.n_examples = n_examples
        self.batch_repeats = batch_repeats if batch_repeats else 1
        self.n_imgs = n_imgs
        self.logdir = logdir
        self.big_image = big_image
        self.w_lambda = w_lambda
        self.w_gamma = w_gamma
        self.epsilon = epsilon
        self.reset_optimizer=reset_optimizer
        self.lipschitz_penalty = lipschitz_penalty
        self.start = True

        # Generate fized latent variables for image previews
        np.random.seed(0)
        self.z_fixed = np.random.normal(size=[self.n_examples, self.z_length])

        # Initialize placeholders
        dtype = tf.uint8 if use_uint8 else tf.float32
        self.x_placeholder = tf.placeholder(dtype, [None, 3, None, None])
        if use_uint8:
            with tf.variable_scope('scale_images'):
                self.x_placeholder = scale_uint8(self.x_placeholder)
        self.z_placeholder = tf.placeholder(tf.float32, [None, self.z_length])

        # Global step
        with tf.variable_scope('global_step'):
            self.global_step = tf.Variable(0, name='global_step', trainable=False)

        # Non-trainable variables for counting to next layer and incrementing value of alpha
        with tf.variable_scope('image_count'):
            self.total_imgs = tf.Variable(0, name='image_step', trainable=False, dtype=tf.int32)
            self.img_step = tf.mod(tf.add(self.total_imgs, self.n_imgs), self.n_imgs * 2)
            self.alpha = tf.minimum(1.0, tf.div(tf.to_float(self.img_step), self.n_imgs))
            self.layer = tf.to_int32(tf.add(self.total_imgs, self.n_imgs) / (self.n_imgs * 2))

        # Initialize optimizer as member variable if not rest_optimizer, otherwise generate new
        # optimizer for each layer
        if self.reset_optimizer:
            self.lr = learning_rate
            self.beta1 = beta1
            self.beta2 = beta2
        else:
            self.g_optimizer = tf.train.AdamOptimizer(learning_rate, beta1, beta2)
            self.d_optimizer = tf.train.AdamOptimizer(learning_rate, beta1, beta2)

        # Initialize FeedDict
        self.feed = FeedDict(imgdir, logdir)
        self.n_layers = self.feed.n_sizes
        self.networks = [self._create_network(i + 1) for i in range(self.n_layers)]

        # Initialize Session, FileWriter and Saver
        self.sess = tf.Session()
        self.sess.run(tf.global_variables_initializer())
        self.writer = tf.summary.FileWriter(self.logdir, graph=self.sess.graph)
        self.saver = tf.train.Saver()

        # Look in logdir to see if a saved model already exists. If so, load it
        try:
            self.saver.restore(self.sess, tf.train.latest_checkpoint(self.logdir))
            print('Restored ----------------\n')
        except Exception:
            pass


    # Function for fading input of current layer into previous layer based on current value of alpha
    def _reparameterize(self, x0, x1):
        return tf.add(
            tf.scalar_mul(tf.subtract(1.0, self.alpha), x0),
            tf.scalar_mul(self.alpha, x1)
        )


    # Function for creating network layout at each layer
    def _create_network(self, layers):

        # Build the generator for this layer
        def generator(z):
            with tf.variable_scope('Generator'):

                with tf.variable_scope('latent_vector'):
                    z = tf.expand_dims(z, 2)
                    g1 = tf.expand_dims(z, 3)

                for i in range(layers):
                    with tf.variable_scope('layer_{}'.format(i)):

                        if i == layers - 1:
                            g0 = g1

                        with tf.variable_scope('1'):
                            if i == 0:
                                g1 = conv_layer(g1, self.channels[i],
                                    filter_size=4, padding='VALID', mode='transpose',
                                    output_shape=[tf.shape(g1)[0], self.channels[i], 4, 4])
                            else:
                                g1 = conv_layer(g1, self.channels[i])

                        with tf.variable_scope('2'):
                            if i == layers - 1:
                                g1 = conv_layer(g1, self.channels[i])
                            else:
                                g1 = conv_layer(g1, self.channels[i], mode='upscale')

                with tf.variable_scope('rgb_layer_{}'.format(layers - 1)):
                    g1 = conv(g1, 3, filter_size=1)

                if layers > 1:
                    with tf.variable_scope('rgb_layer_{}'.format(layers - 2)):
                        g0 = conv(g0, 3, filter_size=1)
                        g = self._reparameterize(g0, g1)
                else:
                    g = g1

            return g

        # Build the discriminator for this layer
        def discriminator(x):
            with tf.variable_scope('Discriminator'):

                if layers > 1:
                    with tf.variable_scope('rgb_layer_{}'.format(layers - 2)):
                        d0 = conv_layer(x, self.channels[layers - 1],
                            filter_size=1, mode='downscale')

                with tf.variable_scope('rgb_layer_{}'.format(layers - 1)):
                    d1 = conv_layer(x, self.channels[layers], filter_size=1)

                for i in reversed(range(layers)):
                    with tf.variable_scope('layer_{}'.format(i)):

                        if i == 0:
                            d1 = minibatch_stddev(d1)

                        with tf.variable_scope('1'):
                            d1 = conv_layer(d1, self.channels[i])

                        with tf.variable_scope('2'):
                            if i == 0:
                                d1 = conv_layer(d1, self.channels[0],
                                    filter_size=4, padding='VALID')
                            else:
                                d1 = conv_layer(d1, self.channels[i], mode='downscale')

                        if i == layers - 1 and layers > 1:
                            d1 = self._reparameterize(d0, d1)

                with tf.variable_scope('dense'):
                    d = tf.reshape(d1, [-1, self.channels[0]])
                    d = dense(d, 1)

            return d

        # image dimensions
        dim = 2 ** (layers + 1)

        # Build the current network
        with tf.variable_scope('Network', reuse=tf.AUTO_REUSE):
            Gz = generator(self.z_placeholder)
            Dz = discriminator(Gz)

            # Mix different resolutions of input images according to value of alpha
            with tf.variable_scope('training_images'):
                x = scale_uint8(self.x_placeholder)
                if layers > 1:
                    x0 = decrese_res(x)
                    x1 = x
                    x = self._reparameterize(x0, x1)

            Dx = discriminator(x)

            # Fake and real image mixing for WGAN-GP loss function
            interp = tf.random_uniform(shape=[tf.shape(Dz)[0], 1, 1, 1], minval=0., maxval=1.)
            x_hat = interp * x + (1 - interp) * Gz
            Dx_hat = discriminator(x_hat)

        # Loss function and scalar summaries
        with tf.variable_scope('Loss_Function'):

            # Wasserstein Distance
            wd = Dz - Dx

            # Gradient/Lipschitz Penalty
            grads = tf.gradients(Dx_hat, [x_hat])[0]
            slopes = tf.sqrt(tf.reduce_sum(tf.square(grads), [1, 2, 3]))
            if self.lipschitz_penalty:
                gp = tf.square(tf.maximum((slopes - self.w_gamma) / self.w_gamma, 0))
            else:
                gp = tf.square((slopes - self.w_gamma) / self.w_gamma)
            gp_scaled = self.w_lambda * gp

            # Epsilon penalty keeps discriminator output for drifting too far away from zero
            epsilon_cost = self.epsilon * tf.square(Dx)

            # Cost and summary scalars
            g_cost = tf.reduce_mean(-Dz)
            d_cost = tf.reduce_mean(wd + gp_scaled + epsilon_cost)
            wd = tf.abs(tf.reduce_mean(wd))
            gp = tf.reduce_mean(gp)

            # Summaries
            wd_sum = tf.summary.scalar('Wasserstein_distance_{}x{}'.format(dim, dim), wd)
            gp_sum = tf.summary.scalar('gradient_penalty_{}x{}'.format(dim, dim), gp)

        # Collecting variables to be trained by optimizers
        g_vars, d_vars = [], []
        var_scopes = ['layer_{}'.format(i) for i in range(layers)]
        var_scopes.extend(['dense', 'rgb_layer_{}'.format(layers - 1), 'rgb_layer_{}'.format(layers - 2)])
        for scope in var_scopes:
            g_vars.extend(tf.get_collection(
                tf.GraphKeys.GLOBAL_VARIABLES,
                scope='Network/Generator/{}'.format(scope)))
            d_vars.extend(tf.get_collection(
                tf.GraphKeys.GLOBAL_VARIABLES,
                scope='Network/Discriminator/{}'.format(scope)))

        # Generate optimizer operations
        # if self.reset_optimizer is True then initialize a new optimizer for each layer
        with tf.variable_scope('Optimize'):
            if self.reset_optimizer:
                g_train = tf.train.AdamOptimizer(
                    self.lr, self.beta1, self.beta2, name='G_optimizer_{}'.format(layers - 1)).minimize(
                    g_cost, var_list=g_vars)
                d_train = tf.train.AdamOptimizer(
                    self.lr, self.beta1, self.beta2, name='D_optimizer_{}'.format(layers - 1)).minimize(
                    d_cost, var_list=d_vars, global_step=self.global_step)

            else:
                g_train = self.g_optimizer.minimize(g_cost, var_list=g_vars)
                d_train = self.d_optimizer.minimize(d_cost, var_list=d_vars, global_step=self.global_step)

            # Increment image count
            n_imgs = tf.shape(x)[0]
            new_image_count = tf.add(self.total_imgs, n_imgs)
            img_step_op = tf.assign(self.total_imgs, new_image_count)
            d_train = tf.group(d_train, img_step_op)

        # Print variable names to before running model
        print([var.name for var in g_vars])
        print([var.name for var in d_vars])

        # Generate preview images
        with tf.variable_scope('image_preview'):
            fake_imgs = tensor_to_imgs(Gz)
            real_imgs = tensor_to_imgs(x[:min(self.batch_sizes[layers - 1], 4)])

            # Upsize images to normal visibility
            if dim < 256:
                fake_imgs = resize_images(fake_imgs, (256, 256))
                real_imgs = resize_images(real_imgs, (256, 256))

            # Concatenate images into one large image for preview, only used if 24 preview images are requested
            if self.big_image and self.n_examples == 24:
                fake_img_list = tf.unstack(fake_imgs, num=24)
                fake_img_list = [tf.concat(fake_img_list[6 * i:6 * (i + 1)], 1) for i in range(4)]
                fake_imgs = tf.concat(fake_img_list, 0)
                fake_imgs = tf.expand_dims(fake_imgs, 0)

                real_img_list = tf.unstack(real_imgs, num=min(self.batch_sizes[layers - 1], 4))
                real_imgs = tf.concat(real_img_list, 1)
                real_imgs = tf.expand_dims(real_imgs, 0)

            # images summaries
            fake_img_sum = tf.summary.image('fake{}x{}'.format(dim, dim), fake_imgs, self.n_examples)
            real_img_sum = tf.summary.image('real{}x{}'.format(dim, dim), real_imgs, 4)

        return (dim, wd, gp, wd_sum, gp_sum, g_train, d_train,
                fake_img_sum, real_img_sum, Gz, discriminator)


    # Summary adding function
    def _add_summary(self, string, gs):
        self.writer.add_summary(string, gs)


    # Latent variable 'z' generator
    def _z(self, batch_size):
        return np.random.normal(0.0, 1.0, [batch_size, self.z_length])


    # Main training function
    def train(self):
        prev_layer = None

        total_imgs = self.sess.run(self.total_imgs)
        max_imgs = (self.n_layers - 0.5) * self.n_imgs * 2

        while total_imgs < max_imgs:

            # Get current layer, global step, alpha and total number of images used so far
            layer, gs, img_step, alpha, total_imgs = self.sess.run([
                self.layer, self.global_step, self.img_step, self.alpha, self.total_imgs])

            # Reset start times if a new layer has begun training
            if layer != prev_layer:
                start_time = dt.datetime.now()
                batch_size = self.batch_sizes[layer]

                # Global step interval to save model and generate image previews
                save_interval = max(1000, 10000 // 2 ** layer)

                # Get network operations and loss functions for current layer
                (dim, wd, gp, wd_sum, gp_sum, g_train, d_train,
                 fake_img_sum, real_img_sum, _, __) = self.networks[layer]

            # Get training data and latent variables to store in feed_dict
            feed_dict = {
                self.x_placeholder: self.feed.next_batch(batch_size, dim),
                self.z_placeholder: self._z(batch_size)
            }

            # Here's where we actually train the model
            for _ in range(self.batch_repeats):
                self.sess.run(g_train, feed_dict)
                self.sess.run(d_train, feed_dict)

            # Get loss values and summaries
            wd_, gp_, wd_sum_str, gp_sum_str = self.sess.run([wd, gp, wd_sum, gp_sum], feed_dict)

            # Print current status, loss functions, etc.
            percent_done = img_step / (2 * self.n_imgs)
            cur_layer_imgs = self.n_imgs * 2

            if layer == 0:
                percent_done = 2 * percent_done - 1
                img_step -= self.n_imgs
                cur_layer_imgs //= 2

            print('dimensions: {}x{} ---- {}% ---- images: {}/{} ---- alpha: {} ---- global step: {}'
                  '\nWasserstein distance: {}\ngradient penalty: {}\n'.format(
                dim, dim, np.round(percent_done * 100, 4), img_step, cur_layer_imgs, alpha, gs, wd_, gp_))

            # Log scalar data every 20 global steps
            if gs % 20 == 0:
                self._add_summary(wd_sum_str, gs)
                self._add_summary(gp_sum_str, gs)

            # Operations to run every save interval
            if gs % save_interval == 0:

                # Do not save the model or generate images immediately after loading/preloading
                if self.start:
                    self.start = False

                # Save the model and generate image previews
                else:
                    print('saving and making images...\n')
                    self.saver.save(
                        self.sess, os.path.join(self.logdir, "model.ckpt"),
                        global_step=self.global_step)

                    real_img_sum_str = self.sess.run(real_img_sum, feed_dict)
                    img_preview_feed_dict = {
                        self.x_placeholder: feed_dict[self.x_placeholder][:4],
                        self.z_placeholder: self.z_fixed
                    }

                    fake_img_sum_str = self.sess.run(fake_img_sum, img_preview_feed_dict)
                    self._add_summary(fake_img_sum_str, gs)
                    self._add_summary(real_img_sum_str, gs)

            # Calculate and print estimated time remaining
            delta_t = dt.datetime.now() - start_time
            time_remaining = delta_t * (1 / (percent_done + 1e-8) - 1)
            print('est. time remaining on current layer: {}'.format(time_remaining))

            prev_layer = layer


    def get_cur_res(self):
        cur_layer = self.sess.run(self.layer)
        return 2 ** (2 + cur_layer)


    # Function for generating images from a 1D or 2D array of latent vectors
    def generate(self, z):
        solo = z.ndim == 1
        if solo:
            z = np.expand_dims(z, 0)

        cur_layer = int(self.sess.run(self.layer))
        imgs = self.networks[cur_layer][9]
        imgs = self.sess.run(imgs, {self.z_placeholder: z})

        if solo:
            imgs = np.squeeze(imgs, 0)
        return imgs


    # def transform(self, input_img, n_iter=100000):
    #     with tf.variable_scope('transform'):
    #         global_step = tf.Variable(0, name='transform_global_step', trainable=False)
    #         transform_img = tf.Variable(input_img, name='transform_img', dtype=tf.float32)
    #
    #     cur_layer = self.sess.run(self.layer)
    #     (dim, wd, gp, wd_sum, gp_sum, g_train, d_train,
    #      ake_img_sum, real_img_sum, Gz, discriminator) = self.networks[cur_layer]
    #
    #     with tf.variable_scope('Network', reuse=tf.AUTO_REUSE):
    #         with tf.variable_scope('resize'):
    #             jitter = tf.random_uniform([2], -10, 10, tf.int32)
    #             img = tf.manip.roll(transform_img, jitter, [1, 2])
    #             img = resize(img, (dim, dim))
    #         Dt = discriminator(img)
    #
    #     t_cost = tf.reduce_mean(-Dt)
    #     tc_sum = tf.summary.scalar('transform_cost_{}x{}'.format(dim, dim), t_cost)
    #     t_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='transform/transform_img')
    #     t_train = tf.train.AdamOptimizer(0.0001).minimize(
    #         t_cost, var_list=t_vars, global_step=global_step)
    #     transform_img_sum = tf.summary.image('transform', transform_img)
    #
    #     self.sess.run(tf.global_variables_initializer())
    #
    #     for i in range(n_iter):
    #         gs, t_cost_, tc_sum_str, _ = self.sess.run([global_step, t_cost, tc_sum, t_train])
    #         print('Global step: {}, cost: {}\n\n'.format(gs, t_cost_))
    #         if i % 20 == 0:
    #             self._add_summary(tc_sum_str, gs)
    #         if i % 1000 == 0:
    #             img_sum_str = self.sess.run(transform_img_sum)
    #             self._add_summary(img_sum_str, gs)


if __name__ == '__main__':
    progan = ProGAN(
        logdir='logdir_v5',
        imgdir='memmaps',
    )
    progan.train()