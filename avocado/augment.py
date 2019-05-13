from astropy.cosmology import FlatLambdaCDM
import numpy as np
import pandas as pd
import string

from .instruments import band_central_wavelengths
from .utils import settings

class Augmentor():
    """Class used to augment a dataset.

    This class takes :class:`AstronomicalObject`s as input and generates new
    :class:`AstronomicalObject`s with the following transformations applied:
    - Drop random observations.
    - Drop large blocks of observations.
    - For galactic observations, adjust the brightness (= distance).
    - For extragalactic observations, adjust the redshift.
    - Add noise.

    The augmentor needs to have some reasonable idea of the properties of the
    survey that it is being applied to. If there is a large dataset that the
    classifier will be used on, then that dataset can be used directly to
    estimate the properties of the survey.

    This class needs to be subclassed to implement survey specific methods.
    These methods are:
    - `_augment_metadata`
    - Either `_choose_sampling_times` or `_choose_target_observation_count`
    - `_simulate_light_curve_uncertainties`
    - `_simulate_detection`

    Parameters
    ----------
    cosmology_kwargs : kwargs (optional)
        Optional parameters to modify the cosmology assumed in the augmentation
        procedure. These kwargs will be passed to
        astropy.cosmology.FlatLambdaCDM.
    """
    def __init__(self, **cosmology_kwargs):
        # Default cosmology to use. This is the one assumed for the PLAsTiCC
        # dataset.
        cosmology_parameters = {
            'H0': 70,
            'Om0': 0.3,
            'Tcmb0': 2.725,
        }

        cosmology_parameters.update(cosmology_kwargs)

        self.cosmology = FlatLambdaCDM(**cosmology_parameters)

    def _augment_metadata(self, reference_object):
        """Generate new metadata for the augmented object.

        This method needs to be implemented in survey-specific subclasses of
        this class.

        Parameters
        ==========
        reference_object : :class:`AstronomicalObject`
            The object to use as a reference for the augmentation.

        Returns
        =======
        augmented_metadata : dict
            The augmented metadata
        """
        return NotImplementedError

    def _choose_target_observation_count(self, augmented_metadata):
        """Choose the target number of observations for a new augmented light
        curve.

        This method needs to be implemented in survey-specific subclasses of
        this class if using the default implementation of
        `_choose_sampling_times`.

        Parameters
        ==========
        augmented_metadata : dict
            The augmented metadata

        Returns
        =======
        target_observation_count : int
            The target number of observations in the new light curve.
        """
        return NotImplementedError

    def _choose_sampling_times(self, reference_object, augmented_metadata,
                              max_time_shift=50, block_width=250,
                              window_padding=100, drop_fraction=0.1):
        """Choose the times at which to sample for a new augmented object.

        This method should really be survey specific, but a default
        implementation is included here that works reasonably well for generic
        light curves. If you are implementing a survey specific version of this
        method, you only need to have the reference_object and
        augmented_metadata parameters. The other parameters are different knobs
        for this method.

        This implementation of _choose_sampling_times requires that the method
        _choose_target_observation_count() be defined that returns how many
        observations we should attempt to have for the new light curve. If a
        different implementation of _choose_sampling_times is used, that method
        may not be required.

        Parameters
        ==========
        reference_object : :class:`AstronomicalObject`
            The object to use as a reference for the augmentation.
        augmented_metadata : dict
            The augmented metadata
        max_time_shift : float (optional)
            The new sampling times will be shifted by up to this amount
            relative to the original ones.
        block_width : float (optional)
            A block of observations with a width specified by this parameter
            will be dropped.
        window_padding : float (optional)
            Observations outside of a window bounded by the first and last
            observations in the reference objects light curve with a padding
            specified by this parameter will be dropped.
        drop_fraction : float (optional)
            This fraction of observations will always be dropped when creating
            the augmented light curve.

        Returns
        =======
        sampling_times : pandas Dataframe
            A pandas Dataframe that has the following columns:
            - time : the times of the simulated observations.
            - band : the bands of the simulated observations.
            - reference_time : the times in the reference light curve that
              correspond to the times of the simulated observations.
        """
        # Figure out the target number of observations to have for the new
        # lightcurve.
        target_observation_count = self._choose_target_observation_count(
            augmented_metadata
        )

        # Start with a copy of the original times and bands.
        reference_observations = reference_object.observations
        sampling_times = reference_observations[['time', 'band']].copy()
        sampling_times['reference_time'] = sampling_times['time'].copy()

        start_time = np.min(sampling_times['time'])
        end_time = np.max(sampling_times['time'])

        # If the redshift changed, shift the time of the observations.
        augmented_redshift = augmented_metadata['redshift']
        reference_redshift = reference_object.metadata['redshift']
        redshift_scale = (1 + augmented_redshift) / (1 + reference_redshift)

        if augmented_redshift != reference_redshift:
            # Shift relative to an approximation of the peak flux time so that
            # we generally keep the interesting part of the light curve in the
            # frame.
            ref_peak_time = (reference_observations['time'].iloc[
                np.argmax(reference_observations['flux'].values)])

            sampling_times['time'] = (
                ref_peak_time +
                redshift_scale * (sampling_times['time'] - ref_peak_time)
            )

        # Shift the observations forward or backward in time by a small
        # amount.
        sampling_times['time'] += np.random.uniform(-max_time_shift,
                                                    max_time_shift)

        # Drop a block of observations corresponding to the typical width of a
        # season to create light curves with large missing chunks.
        block_start = np.random.uniform(start_time-block_width, end_time)
        block_end = block_start + block_width
        block_mask = ((sampling_times['time'] < block_start) |
                      (sampling_times['time'] > block_end))
        sampling_times = sampling_times[block_mask].copy()

        # Drop observations that are outside of the observing window after all
        # of these procedures. We leave a bit of a buffer to get better
        # baselines for background estimation.
        sampling_times = sampling_times[
            (sampling_times['time'] > start_time - window_padding).values &
            (sampling_times['time'] < end_time + window_padding).values
        ].copy()

        # At high redshifts, we need to fill in the light curve to account for
        # the fact that there is a lower observation density compared to lower
        # redshifts.
        num_fill = int(target_observation_count * (redshift_scale - 1))
        if num_fill > 0:
            new_indices = np.random.choice(sampling_times.index, num_fill,
                                           replace=True)
            new_rows = sampling_times.loc[new_indices]

            # Tweak the times of the new rows slightly.
            # UPDATE: Don't do this. Tweaking the times just gives the GP the
            # potential to do bad things, especially for short timescale
            # targets like kilonovae. All that we really care about is getting
            # the correct signal-to-noise for each bin.
            # tweak_scale = 2
            # time_tweaks = np.random.uniform(-tweak_scale, tweak_scale,
            # num_fill)
            # new_rows['time'] += time_tweaks * redshift_scale
            # new_rows['ref_time'] += time_tweaks

            # Choose new bands randomly.
            new_rows['band'] = np.random.choice(reference_object.bands,
                                                num_fill, replace=True)

            sampling_times = pd.concat([sampling_times, new_rows])

        # Drop back down to the target number of observations. Having too few
        # observations is fine, but having too many is not. We always drop at
        # least 10% of observations to get some shakeup of the light curve.
        num_drop = int(max(len(sampling_times) - target_observation_count,
                           drop_fraction * target_observation_count))
        drop_indices = np.random.choice(
            sampling_times.index, num_drop, replace=False
        )
        sampling_times = sampling_times.drop(drop_indices).copy()

        return sampling_times

    def _simulate_light_curve_uncertainties(self, observations,
                                            augmented_metadata):
        """Simulate the observation-related noise for a light curve.

        This method needs to be implemented in survey-specific subclasses of
        this class. It should simulate the observation uncertainties for the
        light curve.

        Parameters
        ==========
        observations : pandas.DataFrame
            The augmented observations that have been sampled from a Gaussian
            Process. These observations have model flux uncertainties listed
            that should be included in the final uncertainties.
        augmented_metadata : dict
            The augmented metadata

        Returns
        =======
        observations : pandas.DataFrame
            The observations with uncertainties added.
        """
        return NotImplementedError

    def _simulate_detection(self, observations, augmented_metadata):
        """Simulate the detection process for a light curve.

        This method needs to be implemented in survey-specific subclasses of
        this class. It should simulate whether each observation is detected as
        a point-source by the survey and set the "detected" flag in the
        observations DataFrame. It should also return whether or not the light
        curve passes a base set of criterion to be included in the sample that
        this classifier will be applied to.

        Parameters
        ==========
        observations : pandas.DataFrame
            The augmented observations that have been sampled from a Gaussian
            Process.
        augmented_metadata : dict
            The augmented metadata

        Returns
        =======
        observations : pandas.DataFrame
            The observations with the detected flag set.
        pass_detection : bool
            Whether or not the full light curve passes the detection thresholds
            used for the full sample.
        """
        return NotImplementedError

    def augment_object(self, reference_object):
        """Generate an augmented version of an object.

        Parameters
        ==========
        reference_object : :class:`AstronomicalObject`
            The object to use as a reference for the augmentation.

        Returns
        =======
        augmented_object : :class:`AstronomicalObject`
            The augmented object.
        """
        # Create a new object id for the augmented object. We choose a random
        # string to add on to the end of the original object id that is very
        # unlikely to have collisions.
        ref_object_id = reference_object.metadata['object_id']
        random_str = ''.join(np.random.choice(list(string.ascii_letters), 10))
        new_object_id = '%s_aug_%s' % (ref_object_id, random_str)

        # Augment the metadata. The details of how this should work is survey
        # specific, so this must be implemented in subclasses.
        augmented_metadata = self._augment_metadata(reference_object)
        augmented_metadata['object_id'] = new_object_id
        augmented_metadata['reference_object_id'] = ref_object_id

        return self._resample_light_curve(reference_object, augmented_metadata)

        # Add observation-related uncertainties to the light_curve
        object_data = _simulate_light_curve_uncertainties(object_model, new_ddf)

        # Model the photoz
        if photoz_reference is not None:
            # Use the reference to simulate the photoz
            object_meta = _simulate_photoz_reference(object_meta,
                                                     photoz_reference)
        else:
            # Use a model of the photoz
            object_meta = _simulate_photoz_model(object_meta)

        # Smear the mwebv value a bit so that it doesn't uniquely identify
        # points. I leave the position on the sky unchanged (ra, dec, etc.).
        # Don't put any of those variables directly into the classifier!
        object_meta['mwebv'] *= np.random.normal(1, 0.1)

        # Update the object id by adding a random fractional offset to the id.
        # This lets us match it to the original but uniquely identify it.
        new_object_id = object_meta['object_id'] + np.random.uniform(0, 1)
        object_data['object_id'] = new_object_id
        object_meta['object_id'] = new_object_id

        object_meta['ddf'] = new_ddf

        if full_return:
            return object_meta, object_data, object_model
        else:
            return object_meta, object_data

    def _resample_light_curve(self, reference_object, augmented_metadata):
        """Resample a light curve as part of the augmenting procedure

        This uses the Gaussian process fit to a light curve to generate new
        simulated observations of that light curve.

        In some cases, the light curve that is generated will be accidentally
        shifted out of the frame, or otherwise missed. If that is the case, the
        light curve will automatically be regenerated with the same metadata
        until it is either detected or until the number of tries has exceeded
        settings['augment_retries'].

        Parameters
        ----------
        reference_object : :class:`AstronomicalObject`
            The object to use as a reference for the augmentation.
        augmented_metadata : dict
            The augmented metadata

        Returns
        -------
        augmented_observations : pandas.DataFrame
            The simulated observations for the augmented object. If the chosen
            metadata leads to an object that is too faint or otherwise unable
            to be detected, None will be returned instead.
        """
        # Get the GP. This uses a cache if possible.
        gp = reference_object.get_default_gaussian_process()

        for attempt in range(settings['augment_retries']):
            # Figure out where to sample the augmented light curve at.
            observations = self._choose_sampling_times(reference_object,
                                                      augmented_metadata)

            # Compute the fluxes from the GP at the augmented observation
            # times.
            new_redshift = augmented_metadata['redshift']
            reference_redshift = reference_object.metadata['redshift']
            redshift_scale = (1 + new_redshift) / (1 + reference_redshift)

            new_wavelengths = np.array([band_central_wavelengths[i] for i in
                                        observations['band']])
            eval_wavelengths = new_wavelengths / redshift_scale
            pred_x_data = np.vstack([observations['time'], eval_wavelengths]).T
            new_fluxes, new_fluxvars = gp(pred_x_data, return_var=True)

            observations['flux'] = new_fluxes
            observations['flux_error'] = np.sqrt(new_fluxvars)

            # Update the brightness of the new observations.
            if reference_redshift == 0:
                # Adjust brightness for galactic objects.
                adjust_mag = np.random.normal(0, 0.5)
                # adjust_mag = np.random.lognormal(-1, 0.5)
                adjust_scale = 10**(-0.4*adjust_mag)
            else:
                # Adjust brightness for extragalactic objects. We simply follow
                # the Hubble diagram.
                delta_distmod = (self.cosmology.distmod(reference_redshift) -
                                 self.cosmology.distmod(new_redshift)).value
                adjust_scale = 10**(0.4*delta_distmod)

            observations['flux'] *= adjust_scale
            observations['flux_error'] *= adjust_scale

            # Add in light curve noise. This is survey specific and must be
            # implemented in subclasses.
            observations = self._simulate_light_curve_uncertainties(
                observations, augmented_metadata)

            # Simulate detection
            observations, pass_detection = self._simulate_detection(
                observations, augmented_metadata)

            # If our light curve passes detection thresholds, we're done!
            if pass_detection:
                return observations

        # Failed to generate valid observations.
        return None