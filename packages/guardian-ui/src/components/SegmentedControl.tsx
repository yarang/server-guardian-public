import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { useControllable } from '../utils/useControllable';

/** One option of a {@link SegmentedControl}. */
export interface SegmentedOption {
  value: string;
  label: string;
}

/** Props for {@link SegmentedControl}. */
export interface SegmentedControlProps {
  options?: SegmentedOption[];
  /** Selected value (controlled). */
  value?: string;
  /** Initial value when uncontrolled. */
  defaultValue?: string;
  /** Called with the new value on selection. */
  onChange?: (value: string) => void;
  /** Accessible name of the group. */
  'aria-label'?: string;
  className?: string;
}

/**
 * Compact single-choice toggle group built from aria-pressed buttons.
 */
export function SegmentedControl({
  options = [],
  value,
  defaultValue,
  onChange,
  className,
  'aria-label': ariaLabel = getUiMessages().segmentedOptions,
}: SegmentedControlProps) {
  const [current, setCurrent] = useControllable<string>(value, defaultValue ?? options[0]?.value ?? '');
  return (
    <div className={cx('sg-seg', className)} role="group" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className="sg-seg__btn"
          aria-pressed={o.value === current}
          onClick={() => {
            setCurrent(o.value);
            onChange?.(o.value);
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
