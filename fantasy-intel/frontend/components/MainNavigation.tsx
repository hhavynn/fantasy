"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  NAV_GROUPS,
  PRIMARY_NAV_ITEMS,
  getActiveRoute,
  type NavGroupId,
} from "@/lib/navigation";

export function MainNavigation() {
  const pathname = usePathname();
  const [openGroup, setOpenGroup] = useState<NavGroupId | null>(null);
  const [mobileOpen, setMobileOpen] = useState(false);

  const navRef = useRef<HTMLDivElement>(null);
  const mobileToggleRef = useRef<HTMLButtonElement>(null);
  const dropdownTriggerRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const { activeHref, activeGroupId } = getActiveRoute(pathname);

  // Reset open dropdown and mobile menu when pathname changes
  const [prevPathname, setPrevPathname] = useState(pathname);
  if (prevPathname !== pathname) {
    setPrevPathname(pathname);
    setOpenGroup(null);
    setMobileOpen(false);
  }

  // Click outside to close open menus
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (navRef.current && !navRef.current.contains(e.target as Node)) {
        setOpenGroup(null);
        setMobileOpen(false);
      }
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        if (openGroup) {
          const trigger = dropdownTriggerRefs.current[openGroup];
          setOpenGroup(null);
          trigger?.focus();
        } else if (mobileOpen) {
          setMobileOpen(false);
          mobileToggleRef.current?.focus();
        }
      }
    }

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [openGroup, mobileOpen]);

  const toggleGroup = (id: NavGroupId) => {
    setOpenGroup((current) => (current === id ? null : id));
  };

  return (
    <div className="main-nav-wrapper" ref={navRef}>
      {/* Desktop navigation bar */}
      <nav className="nav desktop-nav" aria-label="Main Navigation">
        {PRIMARY_NAV_ITEMS.map((item) => {
          const isActive = activeHref === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`nav-link ${isActive ? "active" : ""}`}
              aria-current={isActive ? "page" : undefined}
            >
              {item.label}
            </Link>
          );
        })}

        {NAV_GROUPS.map((group) => {
          const isOpen = openGroup === group.id;
          const isGroupActive = activeGroupId === group.id;

          return (
            <div key={group.id} className="nav-dropdown-container">
              <button
                ref={(el) => {
                  dropdownTriggerRefs.current[group.id] = el;
                }}
                type="button"
                className={`nav-dropdown-trigger ${isGroupActive ? "active" : ""} ${
                  isOpen ? "open" : ""
                }`}
                onClick={() => toggleGroup(group.id)}
                aria-expanded={isOpen}
                aria-haspopup="true"
                aria-controls={`dropdown-menu-${group.id}`}
              >
                <span>{group.label}</span>
                <svg
                  className={`nav-chevron ${isOpen ? "rotated" : ""}`}
                  width="10"
                  height="10"
                  viewBox="0 0 10 10"
                  aria-hidden="true"
                >
                  <path
                    d="M2 3.5L5 6.5L8 3.5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                  />
                </svg>
              </button>

              {isOpen && (
                <div
                  id={`dropdown-menu-${group.id}`}
                  className="nav-dropdown-menu"
                  role="region"
                  aria-label={`${group.label} submenu`}
                >
                  {group.items.map((subItem) => {
                    const isSubActive = activeHref === subItem.href;
                    return (
                      <Link
                        key={subItem.href}
                        href={subItem.href}
                        className={`dropdown-link ${isSubActive ? "active" : ""}`}
                        aria-current={isSubActive ? "page" : undefined}
                        onClick={() => setOpenGroup(null)}
                      >
                        {subItem.label}
                      </Link>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      {/* Mobile toggle button */}
      <button
        ref={mobileToggleRef}
        type="button"
        className={`mobile-menu-toggle ${mobileOpen ? "open" : ""}`}
        onClick={() => setMobileOpen((prev) => !prev)}
        aria-expanded={mobileOpen}
        aria-controls="mobile-nav-panel"
        aria-label="Toggle navigation menu"
      >
        <span className="hamburger-box" aria-hidden="true">
          <span className="hamburger-inner" />
        </span>
      </button>

      {/* Mobile collapsible drawer / panel */}
      {mobileOpen && (
        <div
          id="mobile-nav-panel"
          className="mobile-nav-panel"
          role="region"
          aria-label="Mobile Navigation"
        >
          <div className="mobile-nav-section">
            <span className="mobile-section-title">Navigation</span>
            {PRIMARY_NAV_ITEMS.map((item) => {
              const isActive = activeHref === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`mobile-nav-link ${isActive ? "active" : ""}`}
                  aria-current={isActive ? "page" : undefined}
                  onClick={() => setMobileOpen(false)}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>

          {NAV_GROUPS.map((group) => (
            <div key={group.id} className="mobile-nav-section">
              <span className="mobile-section-title">{group.label}</span>
              {group.items.map((subItem) => {
                const isSubActive = activeHref === subItem.href;
                return (
                  <Link
                    key={subItem.href}
                    href={subItem.href}
                    className={`mobile-nav-link ${isSubActive ? "active" : ""}`}
                    aria-current={isSubActive ? "page" : undefined}
                    onClick={() => setMobileOpen(false)}
                  >
                    {subItem.label}
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
